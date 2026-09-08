"""ASR provider registry."""

from __future__ import annotations

from app.asr.base import ASRProvider, ASRResult, TranscriptSegment, Word, words_to_segments
from app.config import settings

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


def get_provider(name: str | None = None) -> ASRProvider:
    name = (name or settings.asr_provider).lower()

    if name == "elevenlabs":
        from app.asr.elevenlabs import ElevenLabsProvider

        return ElevenLabsProvider()

    if name == "sarvam":
        from app.asr.sarvam import SarvamProvider

        return SarvamProvider()

    if name == "gemini":
        from app.asr.gemini import GeminiProvider

        return GeminiProvider()

    raise ValueError(f"Unknown ASR provider {name!r}. Available: {', '.join(PROVIDERS)}")
