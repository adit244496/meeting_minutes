"""Live recording: the browser streams audio in while the meeting happens."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app import access, features, live, progress, storage
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, MeetingStatus, User
from app.schemas import MeetingOut
from app.worker.tasks import live_transcribe_task, process_meeting_task

router = APIRouter(prefix="/api/meetings", tags=["live"])

# Largest blob accepted per request: ~30 s of browser audio is a few hundred KB.
MAX_CHUNK_BYTES = 20 * 1024 * 1024


def _live_meeting(db: Session, meeting_id: uuid.UUID, user: User) -> Meeting:
    return access.load_meeting(db, meeting_id, user)


@router.post("/{meeting_id}/live/start")
def start_live(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Begin a live recording. Returns whether live transcription is on."""
    meeting = _live_meeting(db, meeting_id, user)
    if meeting.audio_key or meeting.status not in (MeetingStatus.created,):
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting already has a recording")

    enabled = features.is_enabled(db, "live_transcription_enabled")
    if enabled:
        live.start(str(meeting_id))
        meeting.is_live = True
        meeting.live_transcribed_until = 0.0
        db.commit()
        progress.publish(str(meeting_id), "live", 0, "Recording — the live transcript starts after about a minute")
    return {"live": enabled}


@router.post("/{meeting_id}/live/chunk")
async def live_chunk(
    meeting_id: uuid.UUID,
    request: Request,
    seq: int = Query(ge=0),
    ext: str = Query(default="webm", pattern="^(webm|mp4|ogg)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Append the next piece of audio. The body is the raw blob.

    Answers 409 with the expected sequence number when a piece is missing, so
    the browser resends from there.
    """
    meeting = _live_meeting(db, meeting_id, user)
    if not meeting.is_live:
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is not recording live")

    data = await request.body()
    if len(data) > MAX_CHUNK_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Audio piece too large")

    mid = str(meeting_id)
    try:
        next_seq = live.append_chunk(mid, seq, data, ext)
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"message": "Missing audio piece", "expected_seq": exc.args[0]}
        ) from None

    if live.try_queue(mid):
        live_transcribe_task.delay(mid)
    return {"next_seq": next_seq}


@router.post("/{meeting_id}/live/finish", response_model=MeetingOut)
def finish_live(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Meeting:
    """Stop a live recording from the server's copy of the audio.

    For when the tab that was recording has gone - closed, crashed, lost power -
    so it will never upload the complete file. The live buffer holds everything
    received up to the last piece (at most ~30 s short of the end), which is far
    better than losing the meeting.
    """
    meeting = _live_meeting(db, meeting_id, user)
    if not meeting.is_live:
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is not recording")

    mid = str(meeting_id)
    buffer = live.find_buffer(mid)
    meeting.is_live = False
    if buffer is None or buffer.stat().st_size == 0:
        db.commit()
        live.stop(mid)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No audio reached the server for this recording")

    key = f"meetings/{meeting_id}/source{buffer.suffix}"
    storage.put_file(key, buffer, content_type="audio/webm" if buffer.suffix == ".webm" else "audio/mp4")
    meeting.audio_key = key
    meeting.status = MeetingStatus.uploaded
    meeting.error = None
    db.commit()
    live.stop(mid)

    process_meeting_task.delay(mid)
    progress.publish(mid, "queued", 0, "Queued for processing")
    return meeting


@router.post("/{meeting_id}/live/stop")
def stop_live(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """End the live phase. The browser then uploads the complete recording,
    which the normal pipeline turns into the final transcript."""
    meeting = _live_meeting(db, meeting_id, user)
    meeting.is_live = False
    db.commit()
    live.stop(str(meeting_id))
    return {"live": False}
