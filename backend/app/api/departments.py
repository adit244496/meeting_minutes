"""Departments: the unit meeting access is granted by.

Everyone can read the list - a name like "Finance" is not a secret, and the
person recording a meeting has to be able to say which department it belongs
to. Only administrators can create one, rename it, or change who is in it.

See app/access.py for the rule these departments feed.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import access
from app.db import get_db
from app.deps import current_user, require_admin
from app.models import Department, Meeting, User, UserDepartment
from app.schemas import (
    DepartmentAssign,
    DepartmentCreate,
    DepartmentMembers,
    DepartmentOut,
    MeetingOut,
    UserOut,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/departments", tags=["departments"])


def _counts(db: Session) -> tuple[dict, dict]:
    members = dict(
        db.execute(
            select(UserDepartment.department_id, func.count(UserDepartment.user_id)).group_by(
                UserDepartment.department_id
            )
        ).all()
    )
    meetings = dict(
        db.execute(
            select(Meeting.department_id, func.count(Meeting.id))
            .where(Meeting.department_id.is_not(None))
            .group_by(Meeting.department_id)
        ).all()
    )
    return members, meetings


@router.get("", response_model=list[DepartmentOut])
def list_departments(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[DepartmentOut]:
    members, meetings = _counts(db)
    return [
        DepartmentOut(
            id=d.id,
            name=d.name,
            member_count=members.get(d.id, 0),
            meeting_count=meetings.get(d.id, 0),
        )
        for d in db.execute(select(Department).order_by(Department.name)).scalars()
    ]


@router.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: DepartmentCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> DepartmentOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the department a name")
    if db.execute(
        select(Department).where(func.lower(Department.name) == name.lower())
    ).scalars().first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A department with that name already exists")

    department = Department(name=name)
    db.add(department)
    db.commit()
    return DepartmentOut(id=department.id, name=department.name)


@router.patch("/{department_id}", response_model=DepartmentOut)
def rename_department(
    department_id: uuid.UUID,
    payload: DepartmentCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> DepartmentOut:
    department = db.get(Department, department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the department a name")
    clash = db.execute(
        select(Department).where(
            func.lower(Department.name) == name.lower(), Department.id != department_id
        )
    ).scalars().first()
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT, "A department with that name already exists")

    department.name = name
    db.commit()
    members, meetings = _counts(db)
    return DepartmentOut(
        id=department.id,
        name=department.name,
        member_count=members.get(department.id, 0),
        meeting_count=meetings.get(department.id, 0),
    )


@router.delete("/{department_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_department(
    department_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> None:
    """Delete a department. Its meetings survive, unassigned - which means they
    become visible to administrators and their creators only, not to everybody."""
    department = db.get(Department, department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    db.delete(department)
    db.commit()
    log.info("Administrator %s deleted department %s", admin.email, department.name)


@router.get("/{department_id}/members", response_model=list[UserOut])
def list_members(
    department_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[User]:
    department = db.get(Department, department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
    return list(
        db.execute(
            select(User)
            .join(UserDepartment, UserDepartment.user_id == User.id)
            .where(UserDepartment.department_id == department_id)
            .options(selectinload(User.departments))
            .order_by(User.full_name)
        ).scalars()
    )


@router.put("/{department_id}/members", response_model=list[UserOut])
def set_members(
    department_id: uuid.UUID,
    payload: DepartmentMembers,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[User]:
    """Replace the department's membership with exactly these people."""
    department = db.get(Department, department_id)
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    wanted = set(payload.user_ids)
    found = set(
        db.execute(select(User.id).where(User.id.in_(wanted))).scalars()
    ) if wanted else set()
    missing = wanted - found
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such user: {next(iter(missing))}")

    current = {
        row.user_id: row
        for row in db.execute(
            select(UserDepartment).where(UserDepartment.department_id == department_id)
        ).scalars()
    }
    for user_id in current.keys() - wanted:
        db.delete(current[user_id])
    for user_id in wanted - current.keys():
        db.add(UserDepartment(user_id=user_id, department_id=department_id))
    db.commit()
    return list_members(department_id, db, _)


# ---------------------------------------------------------------- assignment


assign_router = APIRouter(prefix="/api/meetings", tags=["departments"])


@assign_router.put("/{meeting_id}/department", response_model=MeetingOut)
def assign_department(
    meeting_id: uuid.UUID,
    payload: DepartmentAssign,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Meeting:
    """Move a meeting into a department, or out of every department.

    A member can only move a meeting they can already see, and only into a
    department they are in - otherwise moving a meeting would be a way to make
    it disappear from your own colleagues, or to plant it on another team.
    Administrators can move anything anywhere.
    """
    meeting = access.load_meeting(db, meeting_id, user)

    if payload.department_id is not None:
        department = db.get(Department, payload.department_id)
        if department is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")
        if not access.is_admin(user) and department.id not in {d.id for d in user.departments}:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "You are not a member of that department"
            )
    elif not access.is_admin(user) and meeting.created_by != user.id:
        # Leaving it unassigned hides it from everyone but admins and whoever
        # recorded it - not something to do to a colleague's meeting.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only an administrator can leave a meeting without a department",
        )

    meeting.department_id = payload.department_id
    db.commit()
    db.refresh(meeting)
    return meeting
