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
from app.minutes.catalog import BY_ID as MINUTES_MODEL_BY_ID
from app.minutes.catalog import GEMINI_MODELS, model_ids
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
    # Enforced: the value must be one of these.
    choices: tuple[str, ...] = ()
    # Offered in a dropdown, not enforced - a model released after this list
    # was written can still be typed in.
    suggestions: tuple[str, ...] = ()


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
            "Writes the minutes when the minutes provider is Anthropic. "
            "Create a key at console.anthropic.com."
        ),
        env_var="ANTHROPIC_API_KEY",
        placeholder="sk-ant-…",
    ),
    Credential(
        key="openai_api_key",
        label="OpenAI",
        description=(
            "Writes the minutes when the minutes provider is OpenAI. "
            "Create a key at platform.openai.com/api-keys."
        ),
        env_var="OPENAI_API_KEY",
        placeholder="sk-…",
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
        suggestions=GEMINI_MODELS,
    ),
    Credential(
        key="minutes_provider",
        label="Minutes provider",
        description="Which service writes the minutes. Existing minutes are untouched.",
        env_var="MINUTES_PROVIDER",
        secret=False,
        choices=("anthropic", "openai"),
    ),
    Credential(
        key="anthropic_model",
        label="Anthropic model",
        description="The Claude model that writes the minutes.",
        env_var="ANTHROPIC_MODEL",
        secret=False,
        placeholder="claude-opus-5",
        suggestions=model_ids("anthropic"),
    ),
    Credential(
        key="openai_model",
        label="OpenAI model",
        description="The OpenAI model that writes the minutes.",
        env_var="OPENAI_MODEL",
        secret=False,
        placeholder="gpt-5",
        suggestions=model_ids("openai"),
    ),
    Credential(
        key="minutes_models",
        label="Models available for regeneration",
        description=(
            "Comma-separated model ids members can pick when regenerating minutes. "
            "A model is only offered when its provider has a key."
        ),
        env_var="MINUTES_MODELS",
        secret=False,
        placeholder="gpt-4o-mini,claude-sonnet-5",
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


# How each provider's keys begin. Only used to catch a key filed under the
# wrong provider - never to reject a key for not matching its own pattern,
# because these formats change and a valid key must always be storable.
#
# ElevenLabs and Sarvam both use "sk_" and are left out: an ambiguous signature
# is worse than none.
_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("sk-ant-", "anthropic_api_key", "an Anthropic"),
    ("AIza", "gemini_api_key", "a Google Gemini"),
    ("sk-", "openai_api_key", "an OpenAI"),
)


def _foreign_key(key: str, value: str) -> str | None:
    """The provider this value actually looks like, when that is not `key`."""
    for prefix, owner, article in _SIGNATURES:
        if value.startswith(prefix):
            return None if owner == key else article
    return None


def rejected(provider_name: str, key: str, value: str) -> RuntimeError:
    """The provider answered 401: a key is configured, and it is not accepted.

    Worth its own message. The SDK's "API key is invalid" says nothing about
    *which* key of the several this app holds, where that key came from, or
    where to change it - and the answer differs depending on whether it was
    typed into the admin panel or set in .env.
    """
    credential = BY_KEY.get(key)
    env_var = credential.env_var if credential else key.upper()
    hint = ""
    if value != value.strip():
        hint = " The stored value has whitespace around it, which is usually the cause."
    elif len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        hint = (
            " The stored value still has its surrounding quotes, which are part of "
            "the key as far as the provider is concerned - remove them from .env."
        )
    return RuntimeError(
        # The mask is of the trimmed value: a newline inside it would break the
        # message across lines, and the hint above already names that problem.
        f"{provider_name} rejected the API key ({mask(value.strip())}, {len(value)} characters). "
        f"Replace it under Settings > AI providers, or fix {env_var} in .env and "
        f"restart both services.{hint}"
    )


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
    suggestions: tuple[str, ...]
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
                suggestions=credential.suggestions,
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
    if credential.secret and value:
        # Caught here rather than at the provider: one key pasted into two
        # fields fails later as a plain 401, which reads as "the key is wrong"
        # rather than "the key is in the wrong box".
        foreign = _foreign_key(key, value)
        if foreign:
            raise ValueError(
                f"That looks like {foreign} key, not {credential.label}'s. "
                f"It starts with {value[:7]!r}. Paste it into the {foreign.split()[-1]} "
                "field instead."
            )
    if key == "minutes_models" and value:
        ids = [part.strip() for part in value.split(",") if part.strip()]
        unknown = [i for i in ids if i not in MINUTES_MODEL_BY_ID]
        if unknown:
            raise ValueError(f"Unknown minutes model(s): {', '.join(unknown)}")
        value = ",".join(dict.fromkeys(ids))

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
