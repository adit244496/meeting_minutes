from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user
from app.minutes.generate import LANGUAGE_NAMES
from app.models import Meeting, MeetingStatus, User
from app.pipeline import generate_and_store_minutes
from app.schemas import MinutesOut

router = APIRouter(prefix="/api/meetings", tags=["minutes"])


@router.get("/{meeting_id}/minutes", response_model=MinutesOut)
def get_minutes(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
):
    meeting = db.execute(
        select(Meeting).where(Meeting.id == meeting_id).options(selectinload(Meeting.minutes))
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if meeting.minutes is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Minutes have not been generated yet")
    return meeting.minutes


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
    """
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
    db.refresh(meeting)
    return meeting.minutes
