from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user
from app.minutes import versions as versions_store
from app.minutes.generate import LANGUAGE_NAMES
from app.models import Meeting, MeetingStatus, Minutes, MinutesVersion, User
from app.pipeline import generate_and_store_minutes
from app.schemas import MinutesOut, MinutesUpdate, MinutesVersionOut

router = APIRouter(prefix="/api/meetings", tags=["minutes"])


def _current_minutes(db: Session, meeting_id: uuid.UUID) -> Minutes:
    minutes = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if minutes is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Minutes have not been generated yet")
    return minutes


@router.get("/{meeting_id}/minutes", response_model=MinutesOut)
def get_minutes(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    return _current_minutes(db, meeting_id)


@router.post("/{meeting_id}/minutes", response_model=MinutesOut)
def regenerate_minutes(
    meeting_id: uuid.UUID,
    language: str | None = Query(default=None, description="en | hi | bn"),
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    """Regenerate minutes from the existing transcript.

    Cheap compared to reprocessing (no ASR call), so this is the right endpoint
    after correcting speaker names or to produce the minutes in another language.
    Any hand-edited version stays in the history and can be restored.
    """
    meeting = db.execute(
        select(Meeting)
        .where(Meeting.id == meeting_id)
        .options(selectinload(Meeting.segments))
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if not meeting.segments:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This meeting has no transcript yet")
    if language and language not in LANGUAGE_NAMES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported language {language!r}. Supported: {', '.join(LANGUAGE_NAMES)}",
        )

    generate_and_store_minutes(db, meeting, output_language=language)
    meeting.status = MeetingStatus.completed
    db.commit()
    return _current_minutes(db, meeting_id)


@router.put("/{meeting_id}/minutes", response_model=MinutesOut)
def edit_minutes(
    meeting_id: uuid.UUID,
    payload: MinutesUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Save a hand-edited version of the minutes.

    The previous state is already in the history, and this edit is appended to
    it, so regenerating later never loses what somebody wrote.
    """
    minutes = _current_minutes(db, meeting_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    return versions_store.apply_edit(db, minutes, changes, user_id=user.id)


@router.get("/{meeting_id}/minutes/versions", response_model=list[MinutesVersionOut])
def list_minutes_versions(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    """Every version, newest first. The first entry is the current one."""
    return versions_store.list_versions(db, meeting_id)


@router.post("/{meeting_id}/minutes/versions/{version}/restore", response_model=MinutesOut)
def restore_minutes_version(
    meeting_id: uuid.UUID,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Make an earlier version current again, as a new version."""
    minutes = _current_minutes(db, meeting_id)
    target = db.execute(
        select(MinutesVersion).where(
            MinutesVersion.meeting_id == meeting_id,
            MinutesVersion.version == version,
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No version {version} for this meeting")

    return versions_store.restore(db, minutes, target, user_id=user.id)
