"""Transcript translations.

The transcript itself is never translated in place - see app/transcripts.py.
These endpoints hand back a cached translation, or queue one.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access, cancel, progress, transcripts
from app.db import get_db
from app.deps import current_user
from app.models import MeetingStatus, Segment, User
from app.worker.celery_app import celery
from app.worker.tasks import translate_transcript_task

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meetings", tags=["transcripts"])

LANGUAGE = Query(pattern="^(en|bn|hi)$", description="en | bn | hi")

# A translation reports every batch; longer silence means the worker died.
STALE_SECONDS = 5 * 60
# The API publishes "queued" itself, so that event proves nothing about the
# worker. This long without the worker replacing it means nothing picked the
# job up - the worker is stopped, or it is busy with an earlier meeting.
UNCLAIMED_SECONDS = 45


@router.get("/{meeting_id}/transcript/translations")
def list_translations(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Which languages are ready, and which one is being made right now."""
    access.load_meeting(db, meeting_id, user)
    state = progress.last_state(str(meeting_id)) or {}
    quiet = progress.seconds_since_update(str(meeting_id))
    running = state.get("stage") == "translate" and quiet is not None and quiet < STALE_SECONDS
    # Still on the event the API published when it queued the job.
    queued = running and int(state.get("percent") or 0) <= 1
    return {
        "languages": transcripts.existing(db, meeting_id),
        "in_progress": running,
        # Which language, so a page opened while somebody else's translation is
        # running shows it rather than offering to start the same job again.
        "language": state.get("language") if running else None,
        "percent": state.get("percent") if running else None,
        "message": state.get("message") if running else None,
        "age_seconds": round(quiet) if running and quiet is not None else None,
        # "Nothing has picked this up", which looks identical to "working on it"
        # from the browser and is the difference between waiting and calling an
        # administrator.
        "unclaimed": bool(queued and quiet is not None and quiet > UNCLAIMED_SECONDS),
    }


@router.get("/{meeting_id}/transcript")
def get_translation(
    meeting_id: uuid.UUID,
    language: str = LANGUAGE,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    access.load_meeting(db, meeting_id, user)
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
    user: User = Depends(current_user),
) -> dict:
    """Queue a translation. Follow GET /{meeting_id}/events for progress."""
    meeting = access.load_meeting(db, meeting_id, user)
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
    progress.publish(mid, "translate", 1, "Queued — waiting for a worker", language=language)
    task = translate_transcript_task.delay(mid, language)
    cancel.remember_task(mid, "translate", task.id)
    return {"queued": True, "language": language}


@router.post("/{meeting_id}/transcript/translate/cancel")
def cancel_translation(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Stop the translation that is running.

    Two mechanisms, because a job can be in one of two places. If it is still
    queued, revoking it means it never runs at all. If a worker already has it,
    the flag stops it at its next checkpoint - within one model call, since the
    batches it has not started are dropped.

    Nothing is stored either way: a half-translated transcript would look
    finished, which is worse than not having one.
    """
    access.load_meeting(db, meeting_id, user)
    mid = str(meeting_id)

    state = progress.last_state(mid) or {}
    quiet = progress.seconds_since_update(mid)
    if not (state.get("stage") == "translate" and quiet is not None and quiet < STALE_SECONDS):
        raise HTTPException(status.HTTP_409_CONFLICT, "No translation is running for this meeting")

    task = cancel.task_id(mid, "translate")
    cancel.request(mid, "translate", task)
    if task:
        try:
            celery.control.revoke(task)
        except Exception:  # noqa: BLE001 - the flag alone still stops a running job
            log.warning("Could not revoke translation task %s", task, exc_info=True)

    # Published now rather than left to the worker: a job that was still queued
    # is revoked and will never report anything itself.
    progress.publish(
        mid, "translate_cancelled", 100, "Translation stopped", language=state.get("language")
    )
    return {"cancelled": True}
