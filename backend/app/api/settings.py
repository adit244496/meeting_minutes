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


class NumberOut(BaseModel):
    key: str
    label: str
    description: str
    unit: str
    minimum: int
    maximum: int
    value: int


class NumberUpdate(BaseModel):
    value: int


# Declared before the generic "/{key}" route below so the paths cannot collide.
@router.get("/numbers", response_model=list[NumberOut])
def list_numbers(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[NumberOut]:
    values = features.all_numbers(db)
    return [
        NumberOut(
            key=n.key,
            label=n.label,
            description=n.description,
            unit=n.unit,
            minimum=n.minimum,
            maximum=n.maximum,
            value=values[n.key],
        )
        for n in features.NUMBERS
    ]


@router.patch("/numbers/{key}", response_model=NumberOut)
def update_number(
    key: str,
    payload: NumberUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> NumberOut:
    try:
        value = features.set_number(db, key, payload.value, user_id=admin.id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such setting: {key}") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    n = features.NUMBER_BY_KEY[key]
    return NumberOut(
        key=n.key,
        label=n.label,
        description=n.description,
        unit=n.unit,
        minimum=n.minimum,
        maximum=n.maximum,
        value=value,
    )


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
