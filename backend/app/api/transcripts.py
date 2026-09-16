"""Transcript translations.

The transcript itself is never translated in place - see app/transcripts.py.
These endpoints hand back a cached translation, or queue one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import progress, transcripts
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, MeetingStatus, Segment, User
from app.worker.tasks import translate_transcript_task

router = APIRouter(prefix="/api/meetings", tags=["transcripts"])

LANGUAGE = Query(pattern="^(en|bn|hi)$", description="en | bn | hi")

# A translation reports every batch; longer silence means the worker died.
STALE_SECONDS = 5 * 60


@router.get("/{meeting_id}/transcript/translations")
def list_translations(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Which languages are ready, and which one is being made right now."""
    state = progress.last_state(str(meeting_id)) or {}
    quiet = progress.seconds_since_update(str(meeting_id))
    running = state.get("stage") == "translate" and quiet is not None and quiet < STALE_SECONDS
    return {
        "languages": transcripts.existing(db, meeting_id),
        "in_progress": running,
        "message": state.get("message") if running else None,
    }


@router.get("/{meeting_id}/transcript")
def get_translation(
    meeting_id: uuid.UUID,
    language: str = LANGUAGE,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    row = transcripts.get(db, meeting_id, language)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"This transcript has not been translated into {language} yet"
        )
    return {
        "language": row.language,
        "model": row.model,
        "created_at": row.created_at.isoformat(),
        "segments": row.segments,
    }


@router.post("/{meeting_id}/transcript/translate", status_code=status.HTTP_202_ACCEPTED)
def translate(
    meeting_id: uuid.UUID,
    language: str = LANGUAGE,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Queue a translation. Follow GET /{meeting_id}/events for progress."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if not db.execute(select(Segment.id).where(Segment.meeting_id == meeting_id).limit(1)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This meeting has no transcript yet")
    if meeting.status in (MeetingStatus.uploaded, MeetingStatus.processing) or meeting.is_live:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Wait for the transcript to finish before translating it"
        )

    mid = str(meeting_id)
    state = progress.last_state(mid) or {}
    quiet = progress.seconds_since_update(mid)
    if state.get("stage") == "translate" and quiet is not None and quiet < STALE_SECONDS:
        raise HTTPException(status.HTTP_409_CONFLICT, "A translation is already running")

    # Published before the task is queued, so a page subscribing right after
    # this sees "queued" rather than a stale earlier event.
    progress.publish(mid, "translate", 1, "Queued — waiting for a worker")
    translate_transcript_task.delay(mid, language)
    return {"queued": True, "language": language}
