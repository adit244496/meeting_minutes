"""Meetings: create, upload audio, browse history, correct speakers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import features, progress, storage
from app.config import settings
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, MeetingStatus, Participant, Segment, User
from app.security import sign_resource, verify_resource
from app.schemas import (
    MeetingCreate,
    MeetingDetail,
    MeetingOut,
    RelabelRequest,
)
from app.worker.tasks import harvest_voiceprint_task, process_meeting_task

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meetings", tags=["meetings"])


def _load(db: Session, meeting_id: uuid.UUID) -> Meeting:
    meeting = db.execute(
        select(Meeting)
        .where(Meeting.id == meeting_id)
        .options(
            selectinload(Meeting.segments),
            selectinload(Meeting.participants),
            selectinload(Meeting.minutes),
        )
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting


@router.post("", response_model=MeetingOut, status_code=status.HTTP_201_CREATED)
def create_meeting(
    payload: MeetingCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Meeting:
    meeting = Meeting(
        title=payload.title,
        source=payload.source,
        language_hint=payload.language_hint,
        asr_provider=payload.asr_provider,
        created_by=user.id,
    )
    db.add(meeting)
    db.commit()
    return meeting


@router.post("/{meeting_id}/audio", response_model=MeetingOut)
def upload_audio(
    meeting_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> Meeting:
    """Attach audio and queue processing.

    Accepts anything ffmpeg can decode - a browser MediaRecorder blob, an m4a
    from a phone, a Zoom cloud recording. Normalisation happens in the worker.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if meeting.status == MeetingStatus.processing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is already processing")

    suffix = (file.filename or "audio").rsplit(".", 1)[-1][:8] or "bin"
    key = f"meetings/{meeting_id}/source.{suffix}"
    storage.put_bytes(key, file.file.read(), content_type=file.content_type or "audio/wav")

    meeting.audio_key = key
    meeting.status = MeetingStatus.uploaded
    meeting.error = None
    db.commit()

    process_meeting_task.delay(str(meeting.id))
    progress.publish(str(meeting.id), "queued", 0, "Queued for processing")
    return meeting


@router.post("/{meeting_id}/reprocess", response_model=MeetingOut)
def reprocess(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> Meeting:
    """Re-run the pipeline - e.g. after enrolling voices that were missing."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if not meeting.audio_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This meeting has no audio")
    if meeting.status == MeetingStatus.processing:
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is already processing")

    meeting.status = MeetingStatus.uploaded
    meeting.error = None
    db.commit()

    process_meeting_task.delay(str(meeting.id))
    return meeting


@router.get("", response_model=list[MeetingOut])
def list_meetings(
    q: str | None = Query(default=None, description="Search titles and transcript text"),
    status_filter: MeetingStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[Meeting]:
    """Meeting history.

    Search is keyword-based over titles, transcripts and summaries. Semantic
    search across history is the intended next step - see README "Searching
    history"; `minutes.embedding` is already in the schema for it.
    """
    stmt = select(Meeting)

    if status_filter:
        stmt = stmt.where(Meeting.status == status_filter)

    if q:
        pattern = f"%{q}%"
        matching = select(Segment.meeting_id).where(Segment.text.ilike(pattern))
        stmt = stmt.where(or_(Meeting.title.ilike(pattern), Meeting.id.in_(matching)))

    stmt = stmt.order_by(Meeting.started_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


@router.get("/{meeting_id}", response_model=MeetingDetail)
def get_meeting(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> Meeting:
    return _load(db, meeting_id)


@router.get("/{meeting_id}/audio-url")
def audio_url(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Hand back a short-lived playback URL.

    The token goes in the query string because <audio src> cannot send an
    Authorization header. It is scoped to this one meeting and expires in 15
    minutes, so a leaked URL exposes one recording briefly rather than the
    bearer's whole session.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if not meeting.audio_key:
        detail = (
            "The recording was deleted under the retention policy"
            if meeting.audio_deleted_at
            else "No audio for this meeting"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    token = sign_resource(str(meeting_id))
    return {"url": f"/api/meetings/{meeting_id}/audio?token={token}"}


@router.get("/{meeting_id}/audio")
def stream_audio(
    meeting_id: uuid.UUID,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream the recording. Authorised by the signed token, not a bearer header."""
    if not verify_resource(str(meeting_id), token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired link")

    meeting = db.get(Meeting, meeting_id)
    if meeting is None or not meeting.audio_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No audio for this meeting")

    try:
        stream = storage.open_stream(meeting.audio_key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file is missing") from None

    suffix = meeting.audio_key.rsplit(".", 1)[-1].lower()
    media_type = {
        "webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
        "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")

    return StreamingResponse(stream, media_type=media_type)


@router.post("/{meeting_id}/speakers", response_model=MeetingDetail)
def relabel_speaker(
    meeting_id: uuid.UUID,
    payload: RelabelRequest,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> Meeting:
    """Correct who a speaker was.

    Corrections are sticky (`is_manual`), so a later reprocess will not undo
    them, and by default the corrected speaker's audio is harvested into a new
    enrollment sample - the system gets better at recognising that person with
    every correction, which is the cheapest possible enrollment path.
    """
    if not features.is_enabled(db, "speaker_relabel_enabled"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Speaker correction is turned off. An administrator can enable it "
            "under Users & Voices.",
        )

    meeting = _load(db, meeting_id)

    participant = next(
        (p for p in meeting.participants if p.speaker_label == payload.speaker_label), None
    )
    if participant is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No speaker {payload.speaker_label!r} in this meeting",
        )

    if payload.user_id:
        user = db.get(User, payload.user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
        participant.user_id = user.id
        participant.display_name = payload.display_name or user.full_name
    else:
        participant.user_id = None
        participant.display_name = payload.display_name or participant.display_name

    participant.is_manual = True
    participant.confidence = 1.0
    db.commit()

    if payload.enroll and participant.user_id:
        harvest_voiceprint_task.delay(
            str(meeting.id), participant.speaker_label, str(participant.user_id)
        )

    return _load(db, meeting_id)


@router.get("/{meeting_id}/events")
async def stream_progress(meeting_id: uuid.UUID) -> StreamingResponse:
    """Server-Sent Events stream of processing progress.

    Note: EventSource cannot set an Authorization header, so this endpoint is
    not behind the JWT dependency. It leaks only stage/percent for a UUID the
    caller must already know. Before production, move this behind a short-lived
    signed query token - see README "Security notes".
    """
    mid = str(meeting_id)

    async def event_stream():
        import redis.asyncio as aioredis

        # Replay the last known state so a late subscriber is not left blank.
        snapshot = progress.last_state(mid)
        if snapshot:
            yield f"data: {json.dumps(snapshot)}\n\n"

        client = aioredis.Redis.from_url(settings.redis_url)
        pubsub = client.pubsub()
        await pubsub.subscribe(progress.channel(mid))
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=15.0
                )
                if message is None:
                    yield ": keep-alive\n\n"  # keeps proxies from closing the stream
                    continue

                data = message["data"]
                text = data.decode() if isinstance(data, bytes) else str(data)
                yield f"data: {text}\n\n"

                try:
                    if json.loads(text).get("stage") in ("done", "failed"):
                        break
                except json.JSONDecodeError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(progress.channel(mid))
            await pubsub.aclose()
            await client.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/stats/overview")
def overview(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    by_status = dict(
        db.execute(select(Meeting.status, func.count(Meeting.id)).group_by(Meeting.status)).all()
    )
    return {
        "total_meetings": int(sum(by_status.values())),
        "by_status": {k.value: v for k, v in by_status.items()},
        "total_hours": round(
            float(db.execute(select(func.coalesce(func.sum(Meeting.duration_seconds), 0.0))).scalar_one())
            / 3600,
            2,
        ),
        "identified_speakers": int(
            db.execute(
                select(func.count(Participant.id)).where(Participant.user_id.is_not(None))
            ).scalar_one()
        ),
    }


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meeting(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> None:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    db.delete(meeting)
    db.commit()
