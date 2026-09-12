"""ASR provider registry."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import credentials
from app.asr.base import ASRProvider, ASRResult, TranscriptSegment, Word, words_to_segments

__all__ = [
    "ASRProvider",
    "ASRResult",
    "TranscriptSegment",
    "Word",
    "words_to_segments",
    "get_provider",
    "PROVIDERS",
]

PROVIDERS = ("elevenlabs", "sarvam", "gemini")


def get_provider(name: str | None = None, db: Session | None = None) -> ASRProvider:
    """Build the configured provider.

    Pass `db` to honour keys and model choices an administrator saved in the
    panel; without it only the environment is consulted. The worker always has a
    session, so a key saved in the UI takes effect on the next meeting with no
    restart.
    """
    name = (name or credentials.resolve(db, "asr_provider")).lower()

    if name == "elevenlabs":
        from app.asr.elevenlabs import ElevenLabsProvider

        return ElevenLabsProvider(api_key=credentials.resolve(db, "elevenlabs_api_key") or None)

    if name == "sarvam":
        from app.asr.sarvam import SarvamProvider

        return SarvamProvider(api_key=credentials.resolve(db, "sarvam_api_key") or None)

    if name == "gemini":
        from app.asr.gemini import GeminiProvider

        return GeminiProvider(
            api_key=credentials.resolve(db, "gemini_api_key") or None,
            model=credentials.resolve(db, "gemini_model") or None,
        )

    raise ValueError(f"Unknown ASR provider {name!r}. Available: {', '.join(PROVIDERS)}")
