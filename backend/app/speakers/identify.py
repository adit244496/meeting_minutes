"""Match diarized speaker clusters to enrolled users.

Design decision worth keeping: we identify **clusters, not frames**.

The diarizer has already decided which turns belong to the same person. Pooling
~30s of that person's audio and making one identification decision per cluster
is far more robust than embedding each short segment and voting - a 1.2-second
"haan, theek hai" carries almost no speaker information, and per-segment
matching lets a handful of those flip a speaker mid-meeting.

Assignment is greedy and one-to-one: the highest-scoring (cluster, user) pair
wins, then both are removed from the pool. Without that constraint two clusters
of similar-sounding colleagues can both be labelled the same person, which is
the failure mode users notice and lose trust over.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.asr.base import TranscriptSegment
from app.audio import extract_clip
from app.config import settings
from app.speakers.embeddings import cosine_similarity, embed_chunks

log = logging.getLogger(__name__)

# Segments shorter than this carry too little speaker information to be useful.
MIN_CLIP_SECONDS = 1.5
# Cap per clip so one long monologue does not crowd out variety.
MAX_CLIP_SECONDS = 10.0


@dataclass
class EnrolledVoice:
    user_id: str
    display_name: str
    embeddings: list[np.ndarray]


@dataclass
class SpeakerResolution:
    speaker_label: str
    user_id: str | None
    display_name: str
    confidence: float
    speaking_seconds: float


def _pick_clips(
    segments: list[TranscriptSegment], budget_seconds: float
) -> list[tuple[float, float]]:
    """Choose the most informative spans for this speaker, up to a time budget."""
    usable = [
        (s.start, min(s.end, s.start + MAX_CLIP_SECONDS))
        for s in segments
        if (s.end - s.start) >= MIN_CLIP_SECONDS
    ]
    # Longest first: the clearest, most sustained speech is the best evidence.
    usable.sort(key=lambda span: span[1] - span[0], reverse=True)

    picked: list[tuple[float, float]] = []
    total = 0.0
    for start, end in usable:
        if total >= budget_seconds:
            break
        picked.append((start, end))
        total += end - start
    return picked


def embed_cluster(
    audio_path: Path,
    segments: list[TranscriptSegment],
    budget_seconds: float | None = None,
) -> np.ndarray | None:
    """Pool a speaker's audio into a single embedding, or None if too little speech."""
    budget = budget_seconds or float(settings.speaker_embed_seconds)
    spans = _pick_clips(segments, budget)
    if not spans:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        clips = [
            extract_clip(audio_path, tmpdir / f"clip_{i:03d}.wav", start, end)
            for i, (start, end) in enumerate(spans)
        ]
        try:
            return embed_chunks(clips)
        except ValueError:
            return None


def label_speakers_only(segments: list[TranscriptSegment]) -> list[SpeakerResolution]:
    """Speaking times and generic names, without any voiceprint matching.

    Used when speaker identification is turned off (the transcript-first phase).
    The diarizer's clusters are still honoured - you get "Speaker 1", "Speaker 2"
    with correct turn boundaries - so the transcript reads properly even before
    anybody has enrolled a voice.
    """
    by_speaker: dict[str, list[TranscriptSegment]] = {}
    for seg in segments:
        by_speaker.setdefault(seg.speaker, []).append(seg)

    return [
        SpeakerResolution(
            speaker_label=label,
            user_id=None,
            display_name=f"Speaker {position}",
            confidence=0.0,
            speaking_seconds=round(sum(s.end - s.start for s in by_speaker[label]), 2),
        )
        for position, label in enumerate(sorted(by_speaker), start=1)
    ]


def identify_speakers(
    audio_path: Path,
    segments: list[TranscriptSegment],
    enrolled: list[EnrolledVoice],
    threshold: float | None = None,
) -> list[SpeakerResolution]:
    """Resolve every diarized cluster in `segments` to a user, or to Unknown."""
    threshold = settings.speaker_match_threshold if threshold is None else threshold

    by_speaker: dict[str, list[TranscriptSegment]] = {}
    for seg in segments:
        by_speaker.setdefault(seg.speaker, []).append(seg)

    speaking = {
        label: sum(s.end - s.start for s in segs) for label, segs in by_speaker.items()
    }

    cluster_vectors: dict[str, np.ndarray] = {}
    for label, segs in by_speaker.items():
        vector = embed_cluster(audio_path, segs)
        if vector is not None:
            cluster_vectors[label] = vector
        else:
            log.info("Speaker %s has too little clean speech to identify", label)

    # Score every (cluster, user) pair. A user's score is their best-matching
    # enrollment sample, not the average - see Voiceprint docstring for why.
    scores: list[tuple[float, str, EnrolledVoice]] = []
    for label, vector in cluster_vectors.items():
        for voice in enrolled:
            best = max(
                (cosine_similarity(vector, e) for e in voice.embeddings), default=-1.0
            )
            scores.append((best, label, voice))
    scores.sort(key=lambda row: row[0], reverse=True)

    assigned_label: dict[str, tuple[EnrolledVoice, float]] = {}
    taken_users: set[str] = set()
    for score, label, voice in scores:
        if score < threshold:
            break  # sorted desc - nothing below here can qualify
        if label in assigned_label or voice.user_id in taken_users:
            continue
        assigned_label[label] = (voice, score)
        taken_users.add(voice.user_id)

    resolutions: list[SpeakerResolution] = []
    for position, label in enumerate(sorted(by_speaker), start=1):
        match = assigned_label.get(label)
        if match:
            voice, score = match
            resolutions.append(
                SpeakerResolution(
                    speaker_label=label,
                    user_id=voice.user_id,
                    display_name=voice.display_name,
                    confidence=round(score, 4),
                    speaking_seconds=round(speaking.get(label, 0.0), 2),
                )
            )
        else:
            resolutions.append(
                SpeakerResolution(
                    speaker_label=label,
                    user_id=None,
                    display_name=f"Unknown Speaker {position}",
                    confidence=0.0,
                    speaking_seconds=round(speaking.get(label, 0.0), 2),
                )
            )
    return resolutions
