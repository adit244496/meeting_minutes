"""ElevenLabs Scribe provider.

Chosen as the default because a single call returns transcript, word-level
timestamps, detected language and diarization together - no second request and
no timestamp alignment work on our side.

The response parsing below is deliberately defensive: verify the current field
names against https://elevenlabs.io/docs/api-reference/speech-to-text before
relying on this in production, and adjust `_parse` if they have moved.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.asr.base import ASRResult, Word, words_to_segments
from app.config import settings

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = "scribe_v1"
# Long meetings are large uploads; the provider still has to run the model.
TIMEOUT = httpx.Timeout(connect=30.0, read=1800.0, write=1800.0, pool=30.0)


class ElevenLabsProvider:
    name = "elevenlabs"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.elevenlabs_api_key
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")

    def transcribe(self, audio_path: Path, language_hint: str | None = None) -> ASRResult:
        data = {
            "model_id": MODEL_ID,
            "diarize": "true",
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
        }
        # Omit the hint for mixed-language meetings - forcing a single language
        # is what produces garbage transliteration on code-switched speech.
        if language_hint:
            data["language_code"] = language_hint

        with audio_path.open("rb") as fh:
            response = httpx.post(
                API_URL,
                headers={"xi-api-key": self.api_key},
                data=data,
                files={"file": (audio_path.name, fh, "audio/wav")},
                timeout=TIMEOUT,
            )
        response.raise_for_status()
        return self._parse(response.json())

    def _parse(self, payload: dict) -> ASRResult:
        language = payload.get("language_code")
        words: list[Word] = []

        for raw in payload.get("words", []) or []:
            # Scribe emits spacing/audio-event entries alongside real words.
            if raw.get("type") not in (None, "word"):
                continue
            text = (raw.get("text") or "").strip()
            if not text:
                continue
            words.append(
                Word(
                    start=float(raw.get("start") or 0.0),
                    end=float(raw.get("end") or 0.0),
                    text=text,
                    speaker=str(raw.get("speaker_id") or "SPEAKER_00"),
                )
            )

        segments = words_to_segments(words, language=language)

        # Fall back to the flat transcript if word timings are unavailable, so a
        # provider-side change degrades into "no diarization" rather than a crash.
        if not segments and payload.get("text"):
            from app.asr.base import TranscriptSegment

            segments = [
                TranscriptSegment(
                    start=0.0,
                    end=0.0,
                    speaker="SPEAKER_00",
                    text=payload["text"].strip(),
                    language=language,
                )
            ]

        return ASRResult(
            provider=self.name,
            segments=segments,
            language=language,
            words=words,
            raw=payload,
        )
