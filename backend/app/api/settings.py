"""Admin-controlled settings: feature toggles, retention windows, API keys."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import credentials, features
from app.minutes import catalog
from app.db import get_db
from app.deps import current_user, require_admin
from app.models import User

log = logging.getLogger(__name__)

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


class CredentialOut(BaseModel):
    """One credential's status. Deliberately never carries the value itself."""

    key: str
    label: str
    description: str
    env_var: str
    secret: bool
    placeholder: str
    choices: list[str]
    suggestions: list[str]
    configured: bool
    source: str
    masked: str
    updated_at: datetime | None


class CredentialUpdate(BaseModel):
    value: str = Field(
        default="",
        description="The new value. Empty clears it, falling back to the environment.",
    )


@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[CredentialOut]:
    """Administrators only, and masked - see the module docstring in credentials.py.

    Unlike the toggles above this is not readable by ordinary members: which
    providers an organisation pays for is not something the meeting UI needs.
    """
    return [CredentialOut(**asdict(s)) for s in credentials.describe(db)]


@router.put("/credentials/{key}", response_model=CredentialOut)
def update_credential(
    key: str,
    payload: CredentialUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
) -> CredentialOut:
    try:
        current = credentials.set_value(db, key, payload.value, user_id=admin.id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such setting: {key}") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except RuntimeError as exc:
        # Encryption unavailable: a gap on the server, not the admin's mistake.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None

    # The name only. Never the value, and never in an exception either.
    log.info(
        "Administrator %s %s credential %r",
        admin.email,
        "cleared" if not payload.value.strip() else "updated",
        key,
    )
    return CredentialOut(**asdict(current))


class MinutesModelOut(BaseModel):
    id: str
    label: str
    provider: str
    default: bool


@router.get("/minutes-models", response_model=list[MinutesModelOut])
def list_minutes_models(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[MinutesModelOut]:
    """Models a member may choose when regenerating minutes.

    Readable by any signed-in user, but only as the usable list: enabled by an
    administrator and backed by a configured key. Which keys exist is not exposed.
    """
    return [MinutesModelOut(**m) for m in catalog.available(db)]


class CatalogModelOut(BaseModel):
    id: str
    label: str
    provider: str
    enabled: bool
    key_configured: bool


class MinutesCatalogOut(BaseModel):
    models: list[CatalogModelOut]
    # True when no list has been saved and the built-in default applies.
    using_default: bool
    default_ids: list[str]


@router.get("/minutes-models/catalog", response_model=MinutesCatalogOut)
def minutes_model_catalog(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> MinutesCatalogOut:
    """Every known minutes model with its enabled and key state, for the admin checklist."""
    enabled = set(catalog.enabled_ids(db))
    keys = catalog.keys_configured(db)
    saved = catalog.parse_list(credentials.resolve(db, "minutes_models"))
    return MinutesCatalogOut(
        models=[
            CatalogModelOut(
                id=m.id,
                label=m.label,
                provider=m.provider,
                enabled=m.id in enabled,
                key_configured=keys.get(m.provider, False),
            )
            for m in catalog.MINUTES_MODELS
        ],
        using_default=not any(i in catalog.BY_ID for i in saved),
        default_ids=list(catalog.DEFAULT_REGENERATION_MODELS),
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
