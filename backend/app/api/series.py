"""Recurring meetings.

A series groups the meetings that recur - the weekly review, the monthly
steering committee. Two things follow from membership:

  * the minutes of each meeting follow up on the previous meeting's action
    items and open questions (see pipeline.previous_meeting_context), and
  * the Series view lists every meeting side by side for comparison.

Nothing is grouped automatically - a wrong link would feed one team's action
items into another team's minutes - but a meeting whose title matches earlier
meetings gets a suggestion the user can accept with one click.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user
from app.models import Meeting, MeetingSeries, Minutes, User
from app.schemas import MeetingOut, SeriesAssign, SeriesOut

router = APIRouter(prefix="/api", tags=["series"])


# Words that differ between occurrences of the same recurring meeting.
_NOISE = re.compile(
    r"\b(\d{1,4}([/.-]\d{1,4}){0,2}|jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|"
    r"aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?|mon(day)?|tue(sday)?|"
    r"wed(nesday)?|thu(rsday)?|fri(day)?|sat(urday)?|sun(day)?|week|wk|q[1-4]|#\d+|no|part|session)\b",
    re.IGNORECASE,
)


def normalise_title(title: str) -> str:
    """"Weekly review - 12 Sep" and "Weekly Review 19/09" both become "weekly review"."""
    text = _NOISE.sub(" ", title.lower())
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def series_name_from_title(title: str) -> str:
    """"weekly Review 19/09" -> "Weekly Review": the title without its dates, as a name."""
    text = _NOISE.sub(" ", title)
    text = re.sub(r"[()\[\]#|/\\,.:;-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:1].upper() + text[1:]) if text else title.strip()


def _series_out(db: Session, series: MeetingSeries) -> SeriesOut:
    count, last = db.execute(
        select(func.count(Meeting.id), func.max(Meeting.started_at)).where(Meeting.series_id == series.id)
    ).one()
    return SeriesOut(id=series.id, name=series.name, meeting_count=count, last_meeting_at=last)


@router.get("/series", response_model=list[SeriesOut])
def list_series(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[SeriesOut]:
    rows = db.execute(
        select(MeetingSeries, func.count(Meeting.id), func.max(Meeting.started_at))
        .outerjoin(Meeting, Meeting.series_id == MeetingSeries.id)
        .group_by(MeetingSeries.id)
        .order_by(func.max(Meeting.started_at).desc().nullslast(), MeetingSeries.name)
    ).all()
    return [
        SeriesOut(id=s.id, name=s.name, meeting_count=count, last_meeting_at=last)
        for s, count, last in rows
    ]


def create_series(db: Session, name: str, user: User) -> MeetingSeries:
    name = name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the series a name")
    # Reuse a series of the same name rather than creating look-alike duplicates.
    existing = db.execute(
        select(MeetingSeries).where(func.lower(MeetingSeries.name) == name.lower())
    ).scalars().first()
    if existing:
        return existing
    series = MeetingSeries(name=name, created_by=user.id)
    db.add(series)
    db.flush()
    return series


@router.put("/meetings/{meeting_id}/series", response_model=MeetingOut)
def assign_series(
    meeting_id: uuid.UUID,
    payload: SeriesAssign,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Meeting:
    """Put a meeting in a series (existing or new by name), or take it out.

    `include_meeting_ids` pulls earlier look-alike meetings in at the same time,
    which is how accepting a suggestion groups the whole history in one step.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")

    if payload.new_series_name:
        series = create_series(db, payload.new_series_name, user)
    elif payload.series_id:
        series = db.get(MeetingSeries, payload.series_id)
        if series is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Series not found")
    else:
        series = None

    meeting.series_id = series.id if series else None
    if series and payload.include_meeting_ids:
        for other in db.execute(
            select(Meeting).where(Meeting.id.in_(payload.include_meeting_ids))
        ).scalars():
            other.series_id = series.id

    db.commit()
    db.refresh(meeting)
    return meeting


@router.get("/meetings/{meeting_id}/series-suggestion")
def series_suggestion(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Earlier meetings whose title looks like the same recurring meeting."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    if meeting.series_id:
        return {"suggest": False}

    key = normalise_title(meeting.title)
    if len(key) < 3:
        return {"suggest": False}

    candidates = db.execute(
        select(Meeting)
        .where(Meeting.id != meeting.id)
        .options(selectinload(Meeting.series))
        .order_by(Meeting.started_at.desc())
        .limit(500)
    ).scalars()
    matches = [m for m in candidates if normalise_title(m.title) == key]
    if not matches:
        return {"suggest": False}

    in_series = next((m.series for m in matches if m.series), None)
    return {
        "suggest": True,
        "series_id": str(in_series.id) if in_series else None,
        "series_name": in_series.name if in_series else series_name_from_title(meeting.title),
        "meetings": [
            {"id": str(m.id), "title": m.title, "started_at": m.started_at.isoformat()}
            for m in matches[:10]
        ],
    }


@router.get("/series/{series_id}/meetings")
def series_meetings(
    series_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Every meeting in the series, newest first, with its minutes for comparison.

    Short minutes are preferred - they are what exists for every meeting and
    they fit side by side; detailed ones are used when there is no short version.
    """
    series = db.get(MeetingSeries, series_id)
    if series is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Series not found")

    meetings = db.execute(
        select(Meeting)
        .where(Meeting.series_id == series_id)
        .options(selectinload(Meeting.minutes))
        .order_by(Meeting.started_at.desc())
    ).scalars().all()

    items = []
    for m in meetings:
        by_kind = {x.kind: x for x in m.minutes}
        chosen: Minutes | None = by_kind.get("short") or by_kind.get("detailed")
        items.append(
            {
                "id": str(m.id),
                "title": m.title,
                "status": m.status.value,
                "started_at": m.started_at.isoformat(),
                "duration_seconds": m.duration_seconds,
                "minutes": None
                if chosen is None
                else {
                    "kind": chosen.kind,
                    "summary": chosen.summary,
                    "key_points": chosen.key_points or [],
                    "decisions": chosen.decisions or [],
                    "action_items": chosen.action_items or [],
                    "open_questions": chosen.open_questions or [],
                    "follow_ups": chosen.follow_ups or [],
                },
            }
        )

    return {"series": _series_out(db, series).model_dump(mode="json"), "meetings": items}


@router.patch("/series/{series_id}", response_model=SeriesOut)
def rename_series(
    series_id: uuid.UUID,
    payload: dict,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> SeriesOut:
    series = db.get(MeetingSeries, series_id)
    if series is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Series not found")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the series a name")
    series.name = name[:255]
    db.commit()
    return _series_out(db, series)
