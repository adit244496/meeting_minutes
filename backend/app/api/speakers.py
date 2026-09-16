"""Voice samples for the speakers in a meeting.

Diarization says "these turns are one person"; it cannot say who. Putting a few
seconds of that voice next to the name is what lets somebody assign it - you
recognise a colleague in two seconds of speech, where reading their words tells
you nothing.

A sample is cut once and cached beside the recording, because the cut needs the
whole meeting audio and a page shows one per speaker.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.audio import extract_sample
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, Segment, User
from app.security import sign_resource, verify_resource

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meetings", tags=["speakers"])

# Enough to recognise a voice without making anyone sit through a monologue.
SAMPLE_SECONDS = 12.0
MIN_SPAN_SECONDS = 1.2
MAX_SPAN_SECONDS = 6.0


def _resource(meeting_id: uuid.UUID, label: str) -> str:
    return f"{meeting_id}:sample:{label}"


def _sample_key(meeting_id: uuid.UUID, label: str) -> str:
    safe = "".join(ch for ch in label if ch.isalnum() or ch in "-_")
    return f"meetings/{meeting_id}/samples/{safe or 'speaker'}.m4a"


@router.get("/{meeting_id}/speakers/samples")
def sample_urls(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """A short-lived playback URL per speaker, for the whole meeting at once."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if not meeting.audio_key:
        return {"samples": {}, "reason": "The recording is no longer available"}

    labels = [
        row[0]
        for row in db.execute(
            select(Segment.speaker_label).where(Segment.meeting_id == meeting_id).distinct()
        )
    ]
    return {
        "samples": {
            label: f"/api/meetings/{meeting_id}/speakers/{label}/sample?token={sign_resource(_resource(meeting_id, label))}"
            for label in labels
        }
    }


@router.get("/{meeting_id}/speakers/{label}/sample")
def speaker_sample(
    meeting_id: uuid.UUID,
    label: str,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    """A few seconds of one speaker, cut from the recording and cached."""
    if not verify_resource(_resource(meeting_id, label), token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link")

    meeting = db.get(Meeting, meeting_id)
    if meeting is None or not meeting.audio_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No recording for this meeting")

    key = _sample_key(meeting_id, label)
    if not storage.exists(key):
        _build_sample(db, meeting, label, key)

    path = storage.local_path(key)
    if path is not None:
        return FileResponse(path, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=3600"})
    body, length, _ = storage.open_range(key, None)
    headers = {"Accept-Ranges": "bytes"}
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(body, media_type="audio/mp4", headers=headers)


def _build_sample(db: Session, meeting: Meeting, label: str, key: str) -> None:
    rows = db.execute(
        select(Segment)
        .where(Segment.meeting_id == meeting.id, Segment.speaker_label == label)
        .order_by(Segment.idx)
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No speech recorded for this speaker")

    # Longest turns first: sustained speech is easiest to recognise a voice in.
    spans = sorted(
        (
            (row.start_ms / 1000, min(row.end_ms / 1000, row.start_ms / 1000 + MAX_SPAN_SECONDS))
            for row in rows
            if (row.end_ms - row.start_ms) / 1000 >= MIN_SPAN_SECONDS
        ),
        key=lambda span: span[1] - span[0],
        reverse=True,
    )
    if not spans:
        # Every turn is short; take them in order rather than giving up.
        spans = [(row.start_ms / 1000, row.end_ms / 1000) for row in rows]

    picked: list[tuple[float, float]] = []
    total = 0.0
    for span in spans:
        if total >= SAMPLE_SECONDS:
            break
        picked.append(span)
        total += span[1] - span[0]
    # Back into meeting order, so the clip runs forwards.
    picked.sort()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        try:
            source = storage.download_to(meeting.audio_key, tmpdir / "source")
        except FileNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file is missing") from None
        clip = extract_sample(source, tmpdir / "sample.m4a", picked)
        storage.put_file(key, clip, content_type="audio/mp4")
    log.info("Built voice sample for %s in meeting %s (%.1fs)", label, meeting.id, total)
