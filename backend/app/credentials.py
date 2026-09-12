"""Provider API keys and model choices an administrator can set from the UI.

Separate from `app/features.py` on purpose. Those are booleans and day counts:
stored in the clear, readable by any signed-in user. These are credentials, so
they are held to a different standard:

  * encrypted at rest, never written to the database in the clear;
  * only an administrator can read or write them;
  * no endpoint ever returns one. The API answers with a mask (last four
    characters) and where the value came from.

Precedence is database over environment. A key in `.env` keeps working
untouched, saving one here overrides it without a restart, and clearing it falls
back to the environment again - so the panel never traps a deployment into
depending on the database for something it used to get from its config.

Nothing here logs a value. If you add logging, log the key's *name*.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting

log = logging.getLogger(__name__)

# Credential rows share the app_settings table with the feature toggles, so they
# are prefixed: no chance of colliding with a toggle key, and easy to pick out.
PREFIX = "cred:"

# Stored values carry their own format tag. Without one there is no way to tell
# an encrypted blob from a value that was written in the clear.
_ENC = "enc:"
_PLAIN = "plain:"


@dataclass(frozen=True)
class Credential:
    """One setting an admin can change from Users & settings.

    `key` is also the attribute name on `Settings`, which is what makes the
    environment fallback in `resolve` work without a lookup table.
    """

    key: str
    label: str
    description: str
    env_var: str
    # Secrets are encrypted and only ever returned masked. Non-secrets (a model
    # name, a provider choice) are stored and returned as typed.
    secret: bool = True
    placeholder: str = ""
    choices: tuple[str, ...] = ()


KEYS: tuple[Credential, ...] = (
    Credential(
        key="gemini_api_key",
        label="Google Gemini",
        description=(
            "Transcription. The default provider, because it handles "
            "code-switched English/Hindi/Bengali better than the alternatives. "
            "Create a key at aistudio.google.com/apikey."
        ),
        env_var="GEMINI_API_KEY",
        placeholder="AIza…",
    ),
    Credential(
        key="anthropic_api_key",
        label="Anthropic",
        description=(
            "Writes the minutes from a finished transcript. Transcription does "
            "not need it - without it, transcripts still work and only minutes "
            "fail, with an explanation."
        ),
        env_var="ANTHROPIC_API_KEY",
        placeholder="sk-ant-…",
    ),
    Credential(
        key="elevenlabs_api_key",
        label="ElevenLabs",
        description=(
            "Alternative transcription provider. Only needed if you switch the "
            "provider below to ElevenLabs."
        ),
        env_var="ELEVENLABS_API_KEY",
        placeholder="sk_…",
    ),
    Credential(
        key="sarvam_api_key",
        label="Sarvam AI",
        description=(
            "Alternative transcription provider, India-specific. Only needed if "
            "you switch the provider below to Sarvam."
        ),
        env_var="SARVAM_API_KEY",
        placeholder="sk_…",
    ),
)

MODELS: tuple[Credential, ...] = (
    Credential(
        key="asr_provider",
        label="Transcription provider",
        description="Which service transcribes new meetings. Existing transcripts are untouched.",
        env_var="ASR_PROVIDER",
        secret=False,
        choices=("gemini", "elevenlabs", "sarvam"),
    ),
    Credential(
        key="gemini_model",
        label="Gemini model",
        description=(
            "The transcription model. Changing this is the quickest way to react "
            "to a model being overloaded or handling mixed-language speech badly."
        ),
        env_var="GEMINI_MODEL",
        secret=False,
        placeholder="gemini-3.6-flash",
    ),
    Credential(
        key="anthropic_model",
        label="Minutes model",
        description="The model that writes the minutes.",
        env_var="ANTHROPIC_MODEL",
        secret=False,
        placeholder="claude-opus-5",
    ),
)

ALL: tuple[Credential, ...] = KEYS + MODELS
BY_KEY = {c.key: c for c in ALL}


# --------------------------------------------------------------------------
# encryption
# --------------------------------------------------------------------------


def encryption_available() -> bool:
    """Whether secrets can be stored at all.

    `cryptography` is in requirements.txt, but an existing deployment may not
    have reinstalled yet. Rather than quietly storing keys in the clear, the API
    refuses to save them and says why - the environment path still works.
    """
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    return True


def _fernet():
    from cryptography.fernet import Fernet

    # Derived from SECRET_KEY rather than kept separately: it is one secret to
    # protect instead of two, and SECRET_KEY already has to be protected. The
    # label domain-separates this from token signing, so the same SECRET_KEY
    # cannot be used interchangeably for both.
    #
    # The consequence is that rotating SECRET_KEY makes stored keys
    # unreadable. `_decode` detects that and says so.
    digest = hashlib.sha256(f"neo-minutes/credentials/{settings.secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encode(value: str) -> str:
    if not encryption_available():
        raise RuntimeError(
            "Cannot save an API key: the 'cryptography' package is not installed "
            "and keys are never stored unencrypted. Install it with "
            "'pip install -r backend/requirements.txt' and restart, or keep "
            "setting keys in .env."
        )
    return _ENC + _fernet().encrypt(value.encode()).decode()


def _decode(stored: str, key: str) -> str:
    """Stored form -> plaintext. Returns "" when it cannot be read."""
    if stored.startswith(_PLAIN):
        return stored[len(_PLAIN) :]
    if not stored.startswith(_ENC):
        # A toggle-era value with no tag. Nothing writes these any more.
        return stored
    if not encryption_available():
        log.warning("Cannot read stored %s: the cryptography package is missing", key)
        return ""

    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(stored[len(_ENC) :].encode()).decode()
    except InvalidToken:
        # The ciphertext is intact but this process cannot derive the same key,
        # which in practice means SECRET_KEY changed since the value was saved.
        # Worth a loud line: falling back to the environment silently would look
        # like the saved key was simply ignored.
        log.warning(
            "Stored %s cannot be decrypted. This usually means SECRET_KEY changed "
            "since it was saved - re-enter the key in Users & settings.",
            key,
        )
        return ""


def mask(value: str) -> str:
    """Enough to recognise a key by, not enough to use."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def resolve(db: Session | None, key: str) -> str:
    """The value to actually use: database first, then the environment.

    `db` may be None for callers with no session (a CLI script, a preflight
    check), which simply means the environment wins.
    """
    credential = BY_KEY.get(key)
    if credential is None:
        raise KeyError(f"unknown credential {key!r}")

    fallback = str(getattr(settings, key, "") or "")
    if db is None:
        return fallback

    row = db.get(AppSetting, PREFIX + key)
    if row is None or not row.value:
        return fallback
    # An unreadable stored value falls back rather than breaking transcription.
    return _decode(row.value, key) or fallback


@dataclass(frozen=True)
class Status:
    """What an administrator is allowed to see about one credential."""

    key: str
    label: str
    description: str
    env_var: str
    secret: bool
    placeholder: str
    choices: tuple[str, ...]
    configured: bool
    # database | environment | unset - which one is in force.
    source: str
    # Masked for secrets; the actual value for model names and provider choices.
    masked: str
    updated_at: datetime | None


def describe(db: Session) -> list[Status]:
    rows = {
        row.key: row
        for row in db.query(AppSetting).filter(AppSetting.key.startswith(PREFIX)).all()
    }

    out: list[Status] = []
    for credential in ALL:
        row = rows.get(PREFIX + credential.key)
        stored = _decode(row.value, credential.key) if row and row.value else ""
        env = str(getattr(settings, credential.key, "") or "")
        value = stored or env

        out.append(
            Status(
                key=credential.key,
                label=credential.label,
                description=credential.description,
                env_var=credential.env_var,
                secret=credential.secret,
                placeholder=credential.placeholder,
                choices=credential.choices,
                configured=bool(value),
                source="database" if stored else "environment" if env else "unset",
                masked=mask(value) if credential.secret else value,
                updated_at=row.updated_at if row and stored else None,
            )
        )
    return out


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def set_value(db: Session, key: str, value: str, user_id=None) -> Status:
    """Save one credential, or clear it to fall back to the environment."""
    credential = BY_KEY.get(key)
    if credential is None:
        raise KeyError(f"unknown credential {key!r}")

    # Outer whitespace only - a pasted key routinely carries a trailing newline,
    # and an invisible space is a miserable thing to debug.
    value = value.strip()
    if credential.choices and value and value not in credential.choices:
        raise ValueError(f"{key} must be one of: {', '.join(credential.choices)}")

    row = db.get(AppSetting, PREFIX + key)

    if not value:
        # Clearing deletes the row so the environment takes over again, rather
        # than storing an empty string that would shadow it.
        if row is not None:
            db.delete(row)
        db.commit()
        return next(s for s in describe(db) if s.key == key)

    stored = _encode(value) if credential.secret else _PLAIN + value
    if row is None:
        db.add(AppSetting(key=PREFIX + key, value=stored, updated_by=user_id))
    else:
        row.value = stored
        row.updated_by = user_id
    db.commit()
    return next(s for s in describe(db) if s.key == key)
