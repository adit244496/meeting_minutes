"""Downloads for each tier of meeting data.

Retention deletes audio, then transcripts, then (rarely) minutes. Anything that
will eventually be deleted has to be exportable first, so each tier gets a
download in a format people actually use elsewhere: plain text and subtitles for
the transcript, Markdown for the minutes, JSON for anything machine-readable.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, Minutes, Participant, Segment, User

router = APIRouter(prefix="/api/meetings", tags=["downloads"])


def _slug(text: str) -> str:
    """A filename stem that survives every OS, without mangling Indic titles."""
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned[:60].strip("-").lower() or "meeting"


def _filename(meeting: Meeting, suffix: str) -> str:
    stamp = (meeting.started_at or datetime.now()).strftime("%Y-%m-%d")
    return f"{stamp}-{_slug(meeting.title)}.{suffix}"


def _attachment(content: str | bytes, filename: str, media_type: str) -> Response:
    body = content.encode("utf-8") if isinstance(content, str) else content
    return Response(
        content=body,
        media_type=media_type,
        headers={
            # RFC 5987 form as well, so a Hindi or Bengali title downloads with
            # its own name rather than a row of question marks.
            "Content-Disposition": (
                f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'download'}\"; "
                f"filename*=UTF-8''{filename}"
            )
        },
    )


def _load(db: Session, meeting_id: uuid.UUID) -> Meeting:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting


def _segments(db: Session, meeting_id: uuid.UUID) -> list[Segment]:
    return list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.idx)
        ).scalars()
    )


def _speaker_names(db: Session, meeting_id: uuid.UUID) -> dict[str, str]:
    return {
        p.speaker_label: p.display_name
        for p in db.execute(
            select(Participant).where(Participant.meeting_id == meeting_id)
        ).scalars()
    }


def _clock(ms: int, srt: bool = False) -> str:
    total, millis = divmod(max(0, ms), 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@router.get("/{meeting_id}/download/transcript")
def download_transcript(
    meeting_id: uuid.UUID,
    fmt: str = Query(default="txt", pattern="^(txt|json|srt)$"),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    meeting = _load(db, meeting_id)
    rows = _segments(db, meeting_id)
    if not rows:
        detail = (
            "The transcript was deleted under the retention policy"
            if meeting.transcript_deleted_at
            else "This meeting has no transcript yet"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    names = _speaker_names(db, meeting_id)

    if fmt == "json":
        body = json.dumps(
            {
                "title": meeting.title,
                "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
                "duration_seconds": meeting.duration_seconds,
                "asr_provider": meeting.asr_provider,
                "segments": [
                    {
                        "start_ms": s.start_ms,
                        "end_ms": s.end_ms,
                        "speaker": names.get(s.speaker_label, s.speaker_label),
                        "speaker_label": s.speaker_label,
                        "language": s.language,
                        "scripts": s.scripts,
                        "text": s.text,
                    }
                    for s in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        return _attachment(body, _filename(meeting, "json"), "application/json; charset=utf-8")

    if fmt == "srt":
        blocks = []
        for i, s in enumerate(rows, start=1):
            who = names.get(s.speaker_label, s.speaker_label)
            blocks.append(
                f"{i}\n{_clock(s.start_ms, srt=True)} --> {_clock(s.end_ms, srt=True)}\n"
                f"{who}: {s.text}\n"
            )
        return _attachment(
            "\n".join(blocks), _filename(meeting, "srt"), "application/x-subrip; charset=utf-8"
        )

    lines = [meeting.title, "=" * len(meeting.title)]
    if meeting.started_at:
        lines.append(meeting.started_at.strftime("%d %B %Y, %H:%M"))
    lines.append("")
    for s in rows:
        who = names.get(s.speaker_label, s.speaker_label)
        lines.append(f"[{_clock(s.start_ms)}] {who}: {s.text}")
    return _attachment(
        "\n".join(lines) + "\n", _filename(meeting, "txt"), "text/plain; charset=utf-8"
    )


@router.get("/{meeting_id}/download/minutes")
def download_minutes(
    meeting_id: uuid.UUID,
    fmt: str = Query(default="md", pattern="^(md|json)$"),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    meeting = _load(db, meeting_id)
    minutes = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if minutes is None:
        detail = (
            "The minutes were deleted under the retention policy"
            if meeting.minutes_deleted_at
            else "Minutes have not been generated yet"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    if fmt == "json":
        body = json.dumps(
            {
                "title": meeting.title,
                "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
                "version": minutes.version,
                "source": minutes.source,
                "model": minutes.model,
                "summary": minutes.summary,
                "topics": minutes.topics,
                "decisions": minutes.decisions,
                "action_items": minutes.action_items,
                "open_questions": minutes.open_questions,
            },
            ensure_ascii=False,
            indent=2,
        )
        return _attachment(body, _filename(meeting, "json"), "application/json; charset=utf-8")

    out = [f"# {meeting.title}", ""]
    if meeting.started_at:
        out.append(meeting.started_at.strftime("%d %B %Y, %H:%M"))
        out.append("")
    out += ["## Summary", "", minutes.summary, ""]

    if minutes.decisions:
        out.append("## Decisions")
        out.append("")
        for d in minutes.decisions:
            who = f" — {d['decided_by']}" if d.get("decided_by") else ""
            out.append(f"- **{d.get('decision', '')}**{who}")
            if d.get("rationale"):
                out.append(f"  - {d['rationale']}")
        out.append("")

    if minutes.action_items:
        out.append("## Action items")
        out.append("")
        for a in minutes.action_items:
            due = f" (due {a['due']})" if a.get("due") else ""
            out.append(f"- [ ] {a.get('task', '')} — {a.get('owner', 'Unassigned')}{due}")
        out.append("")

    if minutes.topics:
        out.append("## Topics")
        out.append("")
        for t in minutes.topics:
            out.append(f"### {t.get('title', '')}")
            out.append("")
            out.append(t.get("discussion", ""))
            out.append("")

    if minutes.open_questions:
        out.append("## Open questions")
        out.append("")
        out += [f"- {q}" for q in minutes.open_questions]
        out.append("")

    out.append(f"*Version {minutes.version} ({minutes.source}), generated by {minutes.model}.*")
    return _attachment(
        "\n".join(out) + "\n", _filename(meeting, "md"), "text/markdown; charset=utf-8"
    )


@router.get("/{meeting_id}/download/audio")
def download_audio(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    meeting = _load(db, meeting_id)
    if not meeting.audio_key:
        detail = (
            "The recording was deleted under the retention policy"
            if meeting.audio_deleted_at
            else "This meeting has no audio"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    try:
        stream = storage.open_stream(meeting.audio_key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file is missing") from None

    suffix = meeting.audio_key.rsplit(".", 1)[-1].lower() or "bin"
    media_type = {
        "webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
        "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")

    filename = _filename(meeting, suffix)
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'recording'}\"; "
                f"filename*=UTF-8''{filename}"
            )
        },
    )
