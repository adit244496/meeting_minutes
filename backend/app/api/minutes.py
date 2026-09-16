from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access, progress
from app.db import get_db
from app.deps import current_user
from app.minutes import catalog
from app.minutes import versions as versions_store
from app.minutes.generate import LANGUAGE_NAMES
from app.models import MeetingStatus, Minutes, MinutesVersion, Segment, User
from app.schemas import MinutesOut, MinutesUpdate, MinutesVersionOut
from app.worker.tasks import generate_minutes_task

router = APIRouter(prefix="/api/meetings", tags=["minutes"])

# A running generation reports at least every few seconds (the heartbeat), so
# this long without an update means the worker died and a new request may start.
MINUTES_STALE_SECONDS = 5 * 60

KIND = Query(default="short", pattern="^(short|detailed)$", description="short | detailed")


def _current_minutes(db: Session, meeting_id: uuid.UUID, kind: str, user: User) -> Minutes:
    # The department check comes first, so a meeting somebody may not see
    # answers the same way whether or not its minutes exist.
    access.load_meeting(db, meeting_id, user)
    minutes = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting_id, Minutes.kind == kind)
    ).scalar_one_or_none()
    if minutes is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{kind.capitalize()} minutes have not been generated yet")
    return minutes


@router.get("/{meeting_id}/minutes", response_model=MinutesOut)
def get_minutes(
    meeting_id: uuid.UUID,
    kind: str = KIND,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return _current_minutes(db, meeting_id, kind, user)


@router.post("/{meeting_id}/minutes", status_code=status.HTTP_202_ACCEPTED)
def regenerate_minutes(
    meeting_id: uuid.UUID,
    language: str | None = Query(default=None, description="en | hi | bn"),
    model: str | None = Query(
        default=None,
        max_length=64,
        description="A model from GET /api/settings/minutes-models. Omit for the admin default.",
    ),
    kind: str = KIND,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Queue minutes generation from the existing transcript.

    Returns immediately; follow GET /{meeting_id}/events for progress, which
    ends with a "minutes_done" or "minutes_failed" event. Cheap compared to
    reprocessing (no ASR call), so this is the right endpoint after correcting
    speaker names or to produce the minutes in another language. Any
    hand-edited version stays in the history and can be restored.
    """
    meeting = access.load_meeting(db, meeting_id, user)
    has_transcript = db.execute(
        select(Segment.id).where(Segment.meeting_id == meeting_id).limit(1)
    ).first()
    if not has_transcript:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This meeting has no transcript yet")
    if language and language not in LANGUAGE_NAMES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported language {language!r}. Supported: {', '.join(LANGUAGE_NAMES)}",
        )
    if model:
        usable = {m["id"] for m in catalog.available(db)}
        if model not in usable:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Model {model!r} is not available for regeneration. "
                "An administrator can enable it under Settings > AI providers.",
            )

    mid = str(meeting_id)
    state = progress.last_state(mid) or {}
    quiet = progress.seconds_since_update(mid)
    if state.get("stage") == "minutes" and quiet is not None and quiet < MINUTES_STALE_SECONDS:
        raise HTTPException(status.HTTP_409_CONFLICT, "Minutes are already being generated")
    if meeting.status in (MeetingStatus.uploaded, MeetingStatus.processing):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Wait for processing to finish before generating minutes"
        )

    # Published before the task is queued, so a page that subscribes right after
    # this returns replays "queued" rather than a stale earlier "done".
    progress.publish(mid, "minutes", 1, f"Queued {kind} minutes — waiting for a worker")
    generate_minutes_task.delay(mid, language, kind, model)
    return {"queued": True}


@router.get("/{meeting_id}/progress")
def current_progress(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """The last progress event, so a reloaded page can pick up a running job."""
    access.load_meeting(db, meeting_id, user)
    state = progress.last_state(str(meeting_id)) or {}
    return {**state, "age_seconds": progress.seconds_since_update(str(meeting_id))}


@router.put("/{meeting_id}/minutes", response_model=MinutesOut)
def edit_minutes(
    meeting_id: uuid.UUID,
    payload: MinutesUpdate,
    kind: str = KIND,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Save a hand-edited version of the minutes.

    The previous state is already in the history, and this edit is appended to
    it, so regenerating later never loses what somebody wrote.
    """
    minutes = _current_minutes(db, meeting_id, kind, user)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    return versions_store.apply_edit(db, minutes, changes, user_id=user.id)


@router.get("/{meeting_id}/minutes/versions", response_model=list[MinutesVersionOut])
def list_minutes_versions(
    meeting_id: uuid.UUID,
    kind: str = KIND,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Every version, newest first. The first entry is the current one."""
    access.load_meeting(db, meeting_id, user)
    rows = versions_store.list_versions(db, meeting_id, kind)
    author_ids = {v.created_by for v in rows if v.created_by}
    names = (
        dict(db.execute(select(User.id, User.full_name).where(User.id.in_(author_ids))).all())
        if author_ids
        else {}
    )
    return [
        MinutesVersionOut.model_validate(v).model_copy(
            update={"created_by_name": names.get(v.created_by)}
        )
        for v in rows
    ]


@router.post("/{meeting_id}/minutes/versions/{version}/restore", response_model=MinutesOut)
def restore_minutes_version(
    meeting_id: uuid.UUID,
    version: int,
    kind: str = KIND,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Make an earlier version current again, as a new version."""
    minutes = _current_minutes(db, meeting_id, kind, user)
    target = db.execute(
        select(MinutesVersion).where(
            MinutesVersion.meeting_id == meeting_id,
            MinutesVersion.kind == kind,
            MinutesVersion.version == version,
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No version {version} for this meeting")

    return versions_store.restore(db, minutes, target, user_id=user.id)
