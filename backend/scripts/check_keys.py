"""Check every provider API key the app would actually use.

Run this when a provider answers 401. It resolves each key exactly the way the
worker does - database first, then the environment - and then tries the
cheapest possible authenticated call against each provider that is configured.
The difference between "no key", "a key that is not accepted" and "the right
key, wrong provider selected" is not visible from the error the browser shows.

    cd backend && venv/bin/python scripts/check_keys.py

"Is the key I pasted the key that got stored?" is a different question, and
--fingerprint answers it. It prints a short hash of each stored key, and with
--compare hashes a key you paste in the same way, so two keys can be checked
for being identical without either one being displayed:

    venv/bin/python scripts/check_keys.py --fingerprint
    venv/bin/python scripts/check_keys.py --compare      # paste the key, press enter

Matching fingerprints mean the stored key is character-for-character the one
you have, and the provider is rejecting the key itself. Different fingerprints
mean something went wrong between the clipboard and the database.

Never prints a key: only its source, length, last four characters, and a hash
that cannot be reversed.
"""

from __future__ import annotations

import getpass
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import credentials  # noqa: E402
from app.db import SessionLocal  # noqa: E402

CHECKS = {
    "gemini_api_key": "Google Gemini",
    "anthropic_api_key": "Anthropic",
    "openai_api_key": "OpenAI",
}


def suspicious(key: str, value: str, seen: dict[str, str]) -> str:
    if value != value.strip():
        return "  <-- has surrounding whitespace, which is part of the key as sent"
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return "  <-- still wrapped in quotes; remove them from .env"
    if "\n" in value or "\r" in value:
        return "  <-- contains a newline"
    if value in seen:
        return f"  <-- the same key is also set as {seen[value]}; one of the two is in the wrong field"
    foreign = credentials._foreign_key(key, value)
    if foreign:
        return f"  <-- this looks like {foreign} key, not this provider's"
    return ""


def try_anthropic(key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "hi"}],
    )
    return "accepted"


def try_openai(key: str) -> str:
    import openai

    openai.OpenAI(api_key=key).models.list()
    return "accepted"


def try_gemini(key: str) -> str:
    from google import genai

    # Held in a local: the client closes its transport when it is collected,
    # and a temporary is collected before the pager has finished with it.
    client = genai.Client(api_key=key)
    next(iter(client.models.list()), None)
    return "accepted"


LIVE = {
    "anthropic_api_key": try_anthropic,
    "openai_api_key": try_openai,
    "gemini_api_key": try_gemini,
}


def fingerprint(value: str) -> str:
    """A short, irreversible hash. Salted with a constant so a fingerprint from
    this tool cannot be matched against a hash from anywhere else."""
    return hashlib.sha256(f"neo-minutes/fingerprint/{value}".encode()).hexdigest()[:12]


def show_fingerprints(db) -> int:
    print("Fingerprints of the keys as stored. Compare with --compare.\n")
    for key, name in CHECKS.items():
        value = credentials.resolve(db, key)
        if not value:
            print(f"{name:<16} no key")
            continue
        # Only the fixed, public part of the prefix - never a secret character.
        # The provider's own console shows the last few, so prefix + last four
        # is enough to identify a key there without revealing anything here.
        head = next(
            (p for p in ("sk-ant-api03-", "sk-proj-", "sk-ant-", "AIza", "AQ.", "sk-") if value.startswith(p)),
            "",
        )
        print(f"{name:<16} {fingerprint(value)}   {head}…{value[-4:]}  {len(value)} chars")
    return 0


def compare() -> int:
    pasted = getpass.getpass("Paste the key you believe is correct (hidden): ").strip()
    if not pasted:
        print("Nothing pasted.")
        return 2
    print(f"\nThat key's fingerprint: {fingerprint(pasted)}   {len(pasted)} chars")
    print("Same fingerprint as the stored one  -> the key was saved exactly as pasted,")
    print("                                       and the provider is rejecting it.")
    print("Different                          -> the wrong key is stored; re-enter it")
    print("                                       under Settings > AI providers.")
    return 0


def main() -> int:
    if "--compare" in sys.argv:
        return compare()

    db = SessionLocal()
    try:
        if "--fingerprint" in sys.argv:
            return show_fingerprints(db)

        status = {s.key: s for s in credentials.describe(db)}
        asr = credentials.resolve(db, "asr_provider") or "gemini"
        minutes = credentials.resolve(db, "minutes_provider") or "anthropic"

        print(f"Transcription provider: {asr}")
        print(f"Minutes provider:       {minutes}   (translations use this one too)")
        print()

        failed = 0
        seen: dict[str, str] = {}
        for key, name in CHECKS.items():
            value = credentials.resolve(db, key)
            source = status[key].source if key in status else "unset"
            if not value:
                print(f"{name:<16} no key ({source})")
                continue

            note = suspicious(key, value, seen)
            seen[value] = name
            print(f"{name:<16} {credentials.mask(value)}  {len(value)} chars, from the {source}{note}")
            try:
                print(f"{'':<16} -> {LIVE[key](value)}")
            except Exception as exc:  # noqa: BLE001 - this is the diagnosis
                failed += 1
                first = str(exc).strip().splitlines()[0][:160]
                print(f"{'':<16} -> REJECTED: {type(exc).__name__}: {first}")

        print()
        in_use = {"gemini": "gemini_api_key", "elevenlabs": "elevenlabs_api_key", "sarvam": "sarvam_api_key"}.get(asr)
        for label, key in (("Transcription", in_use), ("Minutes/translation", f"{minutes}_api_key")):
            if key and not credentials.resolve(db, key):
                print(f"{label} is set to a provider with no key configured.")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
