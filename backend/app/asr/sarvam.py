"""Sarvam AI (Saarika) provider - the Indic-specialist backend.

This is the one to benchmark against ElevenLabs on your own Bengali audio.
Sarvam is built specifically for Indian languages and code-switching, and keeps
data in India, which may matter for your compliance posture.

Caveat, stated plainly: Sarvam's synchronous endpoint is transcription-only.
Speaker diarization lives on their batch/job API, which has a different
submit-then-poll shape. This implementation covers the sync path and returns a
single speaker; `supports_diarization` is False so the pipeline knows to fall
back to per-speaker enrollment matching over the whole file rather than trusting
non-existent cluster labels. Wire the batch API here when you adopt Sarvam as
your primary - verify the current contract at https://docs.sarvam.ai first.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.asr.base import ASRResult, TranscriptSegment
from app.config import settings

API_URL = "https://api.sarvam.ai/speech-to-text"
MODEL_ID = "saarika:v2.5"
TIMEOUT = httpx.Timeout(connect=30.0, read=1800.0, write=1800.0, pool=30.0)

# Sarvam expects BCP-47-ish codes; map our short hints onto them.
LANGUAGE_CODES = {
    "en": "en-IN",
    "hi": "hi-IN",
    "bn": "bn-IN",
}


class SarvamProvider:
    name = "sarvam"
    supports_diarization = False

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.sarvam_api_key
        if not self.api_key:
            raise RuntimeError("SARVAM_API_KEY is not set")

    def transcribe(self, audio_path: Path, language_hint: str | None = None) -> ASRResult:
        # "unknown" asks Sarvam to auto-detect rather than forcing a language.
        code = LANGUAGE_CODES.get(language_hint or "", language_hint) or "unknown"

        with audio_path.open("rb") as fh:
            response = httpx.post(
                API_URL,
                headers={"api-subscription-key": self.api_key},
                data={"model": MODEL_ID, "language_code": code},
                files={"file": (audio_path.name, fh, "audio/wav")},
                timeout=TIMEOUT,
            )
        response.raise_for_status()
        payload = response.json()

        text = (payload.get("transcript") or "").strip()
        language = payload.get("language_code") or language_hint

        segments: list[TranscriptSegment] = []
        if text:
            segments.append(
                TranscriptSegment(
                    start=0.0,
                    end=0.0,
                    speaker="SPEAKER_00",
                    text=text,
                    language=language,
                )
            )

        return ASRResult(
            provider=self.name,
            segments=segments,
            language=language,
            words=[],
            raw=payload,
        )
