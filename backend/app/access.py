"""Who is allowed to see which meeting.

One rule, in one place, so every endpoint that touches a meeting agrees:

  * administrators see everything;
  * everybody else sees the meetings of the departments they belong to, plus
    the meetings they created themselves;
  * a meeting with no department is private to its creator and the admins.

That last clause is what makes the feature safe to switch on. Every meeting
recorded before departments existed has `department_id = NULL`, so turning this
on does not hand a department's history to whoever happens to be in it first -
an administrator assigns each meeting deliberately.

Creator access is not a hole in the rule: it keeps the person who recorded a
meeting from losing it the moment they pick the wrong department, or while they
are not in any department yet.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Session

from app.models import Meeting, Role, User

FORBIDDEN = "This meeting belongs to another department"


def is_admin(user: User) -> bool:
    return user.role == Role.admin


def visible_clause(user: User) -> ColumnElement[bool] | None:
    """A WHERE clause limiting a meetings query to what `user` may see.

    None for administrators, meaning "no restriction" - the caller adds nothing
    to the query rather than a clause that is always true.
    """
    if is_admin(user):
        return None
    ids = [d.id for d in user.departments]
    if not ids:
        return Meeting.created_by == user.id
    return or_(Meeting.department_id.in_(ids), Meeting.created_by == user.id)


def can_view(user: User, meeting: Meeting) -> bool:
    if is_admin(user) or meeting.created_by == user.id:
        return True
    if meeting.department_id is None:
        return False
    return meeting.department_id in {d.id for d in user.departments}


def ensure_view(user: User, meeting: Meeting) -> Meeting:
    """Raise unless `user` may see this meeting."""
    if not can_view(user, meeting):
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    return meeting


def load_meeting(db: Session, meeting_id: uuid.UUID, user: User) -> Meeting:
    """Fetch a meeting the user is allowed to see, or raise 404 / 403."""
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return ensure_view(user, meeting)


def department_for_new_meeting(user: User, requested: uuid.UUID | None) -> uuid.UUID | None:
    """The department a meeting somebody just created belongs to.

    An explicit choice has to be one of their own departments - otherwise
    anyone could file a meeting into a department they cannot read, which is a
    write into someone else's space. With no choice made, a single-department
    member gets that department (the only thing they could have meant) and
    everybody else gets none, leaving the meeting private until it is assigned.
    """
    own = {d.id for d in user.departments}
    if requested is not None:
        if not is_admin(user) and requested not in own:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "You are not a member of that department"
            )
        return requested
    if len(own) == 1:
        return next(iter(own))
    return None
