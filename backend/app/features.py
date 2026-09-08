"""Runtime feature toggles an administrator can flip from the UI.

Distinct from `app/config.py` on purpose. Config settings are deployment
concerns - which ASR provider, where storage lives - and changing one means
editing `.env` and restarting. These are product behaviours an admin decides on,
and they take effect immediately without touching the server.

Every toggle is declared here with a default. Unknown keys are rejected rather
than stored, so a typo in an API call cannot silently create a setting that
nothing reads.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AppSetting


@dataclass(frozen=True)
class Toggle:
    key: str
    default: bool
    label: str
    description: str


TOGGLES: tuple[Toggle, ...] = (
    Toggle(
        key="speaker_relabel_enabled",
        default=False,
        label="Let people correct speaker names",
        description=(
            "Shows a control on each meeting for reassigning a speaker to a "
            "different person. Corrections also enroll that speaker's voice, so "
            "future meetings recognise them automatically."
        ),
    ),
    Toggle(
        key="voice_enrollment_enabled",
        default=False,
        label="Voice enrollment",
        description=(
            "Lets administrators upload voice samples so the system can put real "
            "names to speakers. Requires the image to be built with speaker "
            "identification support - see WITH_SPEAKER_ID in the README."
        ),
    ),
)

BY_KEY = {toggle.key: toggle for toggle in TOGGLES}


def is_enabled(db: Session, key: str) -> bool:
    toggle = BY_KEY.get(key)
    if toggle is None:
        raise KeyError(f"unknown feature toggle {key!r}")

    row = db.get(AppSetting, key)
    if row is None:
        return toggle.default
    return row.value.lower() in ("1", "true", "yes", "on")


def all_values(db: Session) -> dict[str, bool]:
    stored = {row.key: row.value for row in db.query(AppSetting).all()}
    return {
        t.key: stored.get(t.key, str(t.default)).lower() in ("1", "true", "yes", "on")
        for t in TOGGLES
    }


def set_enabled(db: Session, key: str, enabled: bool, user_id=None) -> bool:
    if key not in BY_KEY:
        raise KeyError(f"unknown feature toggle {key!r}")

    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(enabled).lower())
        db.add(row)
    else:
        row.value = str(enabled).lower()
    row.updated_by = user_id
    db.commit()
    return enabled
