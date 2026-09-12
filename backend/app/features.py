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


@dataclass(frozen=True)
class Number:
    """A numeric setting an admin can change without a redeploy."""

    key: str
    default: int
    label: str
    description: str
    unit: str = "days"
    minimum: int = 0
    maximum: int = 3650


# Retention, one tier at a time. Audio is by far the largest and least
# re-readable artifact, so it goes first; the transcript is small text; the
# minutes are the thing people actually come back to, so they default to
# forever. 0 means "keep forever" everywhere here.
NUMBERS: tuple[Number, ...] = (
    Number(
        key="retention_days_recordings",
        default=7,
        label="Keep recordings for",
        description=(
            "Audio files are deleted after this many days. The transcript and "
            "minutes for those meetings are kept. 0 keeps recordings forever."
        ),
    ),
    Number(
        key="retention_days_transcripts",
        default=30,
        label="Keep transcripts for",
        description=(
            "The word-by-word transcript is deleted after this many days. The "
            "minutes survive, so the record of what was decided remains. "
            "0 keeps transcripts forever."
        ),
    ),
    Number(
        key="retention_days_minutes",
        default=0,
        label="Keep minutes for",
        description=(
            "Minutes and their edit history. 0 keeps them forever, which is the "
            "default - they are small and are usually the reason to keep a "
            "meeting at all."
        ),
    ),
)

NUMBER_BY_KEY = {number.key: number for number in NUMBERS}


def get_number(db: Session, key: str) -> int:
    number = NUMBER_BY_KEY.get(key)
    if number is None:
        raise KeyError(f"unknown numeric setting {key!r}")

    row = db.get(AppSetting, key)
    if row is None:
        return number.default
    try:
        return max(number.minimum, min(number.maximum, int(row.value)))
    except (TypeError, ValueError):
        # A malformed stored value must not disable retention silently.
        return number.default


def all_numbers(db: Session) -> dict[str, int]:
    return {number.key: get_number(db, number.key) for number in NUMBERS}


def set_number(db: Session, key: str, value: int, user_id=None) -> int:
    number = NUMBER_BY_KEY.get(key)
    if number is None:
        raise KeyError(f"unknown numeric setting {key!r}")
    if not number.minimum <= value <= number.maximum:
        raise ValueError(
            f"{key} must be between {number.minimum} and {number.maximum}, got {value}"
        )

    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=str(value))
        db.add(row)
    else:
        row.value = str(value)
    row.updated_by = user_id
    db.commit()
    return value


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
