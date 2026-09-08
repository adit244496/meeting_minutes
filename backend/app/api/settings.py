"""Admin-controlled feature toggles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import features
from app.db import get_db
from app.deps import current_user, require_admin
from app.models import User

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ToggleOut(BaseModel):
    key: str
    label: str
    description: str
    enabled: bool


class ToggleUpdate(BaseModel):
    enabled: bool


@router.get("", response_model=list[ToggleOut])
def list_toggles(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[ToggleOut]:
    """Readable by any signed-in user - the UI needs it to hide disabled controls."""
    values = features.all_values(db)
    return [
        ToggleOut(
            key=t.key, label=t.label, description=t.description, enabled=values[t.key]
        )
        for t in features.TOGGLES
    ]


@router.patch("/{key}", response_model=ToggleOut)
def update_toggle(
    key: str,
    payload: ToggleUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ToggleOut:
    try:
        features.set_enabled(db, key, payload.enabled, user_id=admin.id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such setting: {key}") from None

    toggle = features.BY_KEY[key]
    return ToggleOut(
        key=toggle.key,
        label=toggle.label,
        description=toggle.description,
        enabled=payload.enabled,
    )
