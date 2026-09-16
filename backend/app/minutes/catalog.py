"""The models an administrator can pick for writing minutes.

One list serves three places: the model dropdowns in AI providers, the
"available for regeneration" checklist, and the model picker on a meeting page.
A model is only offered on a meeting page when its provider has a key, so an
admin can enable a model before buying the key without breaking the page.
"""

from __future__ import annotations

from dataclasses import dataclass

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# credentials.py imports the model lists below for its dropdowns, so the reverse
# import happens inside the functions that need it.


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    provider: str  # anthropic | openai


MINUTES_MODELS: tuple[ModelOption, ...] = (
    ModelOption("claude-opus-5", "Claude Opus 5", "anthropic"),
    ModelOption("claude-sonnet-5", "Claude Sonnet 5", "anthropic"),
    ModelOption("claude-haiku-4-5-20251001", "Claude Haiku 4.5", "anthropic"),
    ModelOption("gpt-5", "GPT-5", "openai"),
    ModelOption("gpt-5-mini", "GPT-5 mini", "openai"),
    ModelOption("gpt-4.1", "GPT-4.1", "openai"),
    ModelOption("gpt-4.1-mini", "GPT-4.1 mini", "openai"),
    ModelOption("gpt-4o", "GPT-4o", "openai"),
    ModelOption("gpt-4o-mini", "GPT-4o mini", "openai"),
)
BY_ID = {m.id: m for m in MINUTES_MODELS}

# Offered for regeneration until an administrator chooses otherwise: one cheap
# fast model and one strong one, filtered to whichever provider has a key.
DEFAULT_REGENERATION_MODELS: tuple[str, ...] = ("gpt-4o-mini", "claude-sonnet-5")

GEMINI_MODELS: tuple[str, ...] = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash")

_KEY_FOR = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}
_MODEL_FOR = {"anthropic": "anthropic_model", "openai": "openai_model"}


def model_ids(provider: str) -> tuple[str, ...]:
    return tuple(m.id for m in MINUTES_MODELS if m.provider == provider)


def parse_list(raw: str) -> list[str]:
    seen: list[str] = []
    for part in (raw or "").split(","):
        item = part.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def enabled_ids(db: Session | None) -> list[str]:
    """The admin's regeneration list, or the default when none is saved."""
    from app import credentials

    raw = credentials.resolve(db, "minutes_models")
    chosen = [i for i in parse_list(raw) if i in BY_ID]
    return chosen or list(DEFAULT_REGENERATION_MODELS)


def keys_configured(db: Session | None) -> dict[str, bool]:
    from app import credentials

    return {p: bool(credentials.resolve(db, k)) for p, k in _KEY_FOR.items()}


def default_model(db: Session | None) -> tuple[str, str]:
    """(provider, model) the admin set as the main minutes writer."""
    from app import credentials

    provider = (credentials.resolve(db, "minutes_provider") or "anthropic").lower()
    if provider not in _MODEL_FOR:
        provider = "anthropic"
    return provider, credentials.resolve(db, _MODEL_FOR[provider])


def available(db: Session | None) -> list[dict]:
    """Models a member may pick on a meeting page: enabled and with a key."""
    keys = keys_configured(db)
    _, default = default_model(db)
    out = []
    for model_id in enabled_ids(db):
        option = BY_ID[model_id]
        if keys.get(option.provider):
            out.append(
                {
                    "id": option.id,
                    "label": option.label,
                    "provider": option.provider,
                    "default": option.id == default,
                }
            )
    return out


def provider_of(model_id: str) -> str | None:
    option = BY_ID.get(model_id)
    return option.provider if option else None
