"""The ASR provider seam.

Every hosted provider returns a slightly different JSON shape, and none of them
returns speaker *identity* - only anonymous diarization labels. This module
defines the one shape the rest of the app knows about, so swapping ElevenLabs
for Sarvam (or adding Deepgram, Google, Azure) touches exactly one file.

Benchmark note: implement two providers, run the same 10-minute Bengali/Hindi
sample through both, and diff the transcripts. That measurement - not vendor
marketing - should decide your default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# Gap between consecutive words of the same speaker that forces a new segment.
SEGMENT_GAP_SECONDS = 0.8
# Hard cap so a monologue does not become one unreadable wall of text.
MAX_SEGMENT_SECONDS = 30.0

UNKNOWN_SPEAKER = "SPEAKER_00"


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: str = UNKNOWN_SPEAKER


@dataclass
class TranscriptSegment:
    start: float
    end: float
    speaker: str
    text: str
    language: str | None = None
    confidence: float | None = None


@dataclass
class ASRResult:
    provider: str
    segments: list[TranscriptSegment]
    language: str | None = None
    words: list[Word] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def speaker_labels(self) -> list[str]:
        return sorted({s.speaker for s in self.segments})


@runtime_checkable
class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio_path: Path, language_hint: str | None = None) -> ASRResult:
        """Transcribe with word timestamps and speaker diarization."""
        ...


def words_to_segments(
    words: list[Word],
    language: str | None = None,
    gap_seconds: float = SEGMENT_GAP_SECONDS,
    max_seconds: float = MAX_SEGMENT_SECONDS,
) -> list[TranscriptSegment]:
    """Group word-level output into speaker turns.

    Providers give word-level speaker tags; humans read speaker turns. Break on
    speaker change, on a silence longer than `gap_seconds`, or when a turn runs
    past `max_seconds`.
    """
    segments: list[TranscriptSegment] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        segments.append(
            TranscriptSegment(
                start=current[0].start,
                end=current[-1].end,
                speaker=current[0].speaker,
                text=" ".join(w.text for w in current).strip(),
                language=language,
            )
        )
        current.clear()

    for word in words:
        if not word.text.strip():
            continue
        if current:
            same_speaker = word.speaker == current[0].speaker
            gap = word.start - current[-1].end
            too_long = (word.end - current[0].start) > max_seconds
            if not same_speaker or gap > gap_seconds or too_long:
                flush()
        current.append(word)

    flush()
    return [s for s in segments if s.text]
