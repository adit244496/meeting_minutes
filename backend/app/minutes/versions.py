"""Version history for meeting minutes.

Minutes are the one artifact people edit by hand, and regeneration is a button
anyone can press - so an edit must never be silently overwritten. Every state
the minutes have ever been in is written to `minutes_versions`, and the row in
`minutes` is simply whichever version is current.

Content is stored whole rather than as diffs. Minutes are a few kilobytes, and a
self-contained row means restoring a version never has to replay a chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Minutes, MinutesVersion

# The fields that make up the body of a set of minutes, shared by the current
# row and every historical version.
CONTENT_FIELDS = (
    "summary",
    "topics",
    "decisions",
    "action_items",
    "open_questions",
    "languages_detected",
    "key_points",
    "follow_ups",
)


def content_of(minutes: Minutes | MinutesVersion) -> dict:
    return {field: getattr(minutes, field) for field in CONTENT_FIELDS}


def next_version(db: Session, meeting_id: uuid.UUID, kind: str) -> int:
    """Versions count separately for short and detailed minutes."""
    highest = db.execute(
        select(func.max(MinutesVersion.version)).where(
            MinutesVersion.meeting_id == meeting_id, MinutesVersion.kind == kind
        )
    ).scalar()
    return (highest or 0) + 1


def record(
    db: Session,
    meeting_id: uuid.UUID,
    minutes: Minutes,
    source: str,
    user_id: uuid.UUID | None = None,
) -> MinutesVersion:
    """Snapshot the current minutes into the history.

    Call this after writing `minutes`, so the history always contains the state
    that is live right now - the newest version row and the current row agree.
    """
    version = MinutesVersion(
        meeting_id=meeting_id,
        kind=minutes.kind,
        version=minutes.version,
        model=minutes.model,
        source=source,
        created_by=user_id,
        **content_of(minutes),
    )
    db.add(version)
    db.flush()
    return version


def list_versions(db: Session, meeting_id: uuid.UUID, kind: str) -> list[MinutesVersion]:
    return list(
        db.execute(
            select(MinutesVersion)
            .where(MinutesVersion.meeting_id == meeting_id, MinutesVersion.kind == kind)
            .order_by(MinutesVersion.version.desc())
        ).scalars()
    )


def apply_edit(
    db: Session,
    minutes: Minutes,
    changes: dict,
    user_id: uuid.UUID | None = None,
) -> Minutes:
    """Replace the current minutes with a hand-edited version.

    Only the fields present in `changes` are touched, so saving an edited
    summary does not blank out the action items.
    """
    for field, value in changes.items():
        if field in CONTENT_FIELDS and value is not None:
            setattr(minutes, field, value)

    minutes.version = next_version(db, minutes.meeting_id, minutes.kind)
    minutes.source = "edited"
    minutes.edited_at = datetime.now(timezone.utc)
    minutes.edited_by = user_id
    db.flush()

    record(db, minutes.meeting_id, minutes, source="edited", user_id=user_id)
    db.commit()
    return minutes


def restore(
    db: Session,
    minutes: Minutes,
    version: MinutesVersion,
    user_id: uuid.UUID | None = None,
) -> Minutes:
    """Bring an earlier version back as the current one.

    The restore is itself a new version rather than a rewind, so the history
    stays append-only and you can always see that a restore happened.
    """
    for field, value in content_of(version).items():
        setattr(minutes, field, value)

    minutes.version = next_version(db, minutes.meeting_id, minutes.kind)
    minutes.source = "restored"
    minutes.edited_at = datetime.now(timezone.utc)
    minutes.edited_by = user_id
    db.flush()

    record(db, minutes.meeting_id, minutes, source="restored", user_id=user_id)
    db.commit()
    return minutes
