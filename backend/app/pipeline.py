"""The processing pipeline, end to end.

    audio -> [1] normalise -> [2] hosted ASR (transcript + diarization)
                                        |
                          [3] match clusters to voiceprints   (optional)
                                        |
                          [4] persist transcript + participants
                                        |
                          [5] Claude -> structured minutes     (optional)

Stages 2 and 5 cost money; stages 1 and 3 are local CPU work.

Stages 3 and 5 are gated behind AUTO_IDENTIFY_SPEAKERS and AUTO_GENERATE_MINUTES,
both off by default. That makes the default run a transcript-only pipeline, which
is the right place to start: it isolates ASR quality - the one thing that can sink
the project - from everything layered on top of it. Turn each on as the layer
below it proves out.

Everything here is synchronous - it runs inside a Celery worker, not a request.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import progress, storage
from app.asr import get_provider
from app.audio import duration_seconds, to_wav16k_mono
from app.config import settings
from app.lang import analyse
from app.minutes.generate import generate_minutes
from app.models import (
    Meeting,
    MeetingStatus,
    Minutes,
    Participant,
    Segment,
    User,
    Voiceprint,
)
from app.speakers.identify import EnrolledVoice, identify_speakers, label_speakers_only

log = logging.getLogger(__name__)


def load_enrolled_voices(db: Session) -> list[EnrolledVoice]:
    """Every active user who has at least one enrolled voice sample."""
    rows = db.execute(
        select(User, Voiceprint)
        .join(Voiceprint, Voiceprint.user_id == User.id)
        .where(User.is_active.is_(True))
    ).all()

    by_user: dict[str, EnrolledVoice] = {}
    for user, voiceprint in rows:
        key = str(user.id)
        if key not in by_user:
            by_user[key] = EnrolledVoice(
                user_id=key, display_name=user.full_name, embeddings=[]
            )
        by_user[key].embeddings.append(np.asarray(voiceprint.embedding, dtype=np.float32))
    return list(by_user.values())


def process_meeting(db: Session, meeting_id: uuid.UUID) -> None:
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"meeting {meeting_id} not found")
    if not meeting.audio_key:
        raise ValueError(f"meeting {meeting_id} has no audio")

    mid = str(meeting_id)
    meeting.status = MeetingStatus.processing
    meeting.error = None
    db.commit()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # [1] Fetch and normalise -------------------------------------
            progress.publish(mid, "download", 5, "Fetching audio")
            raw = storage.download_to(meeting.audio_key, tmpdir / "raw_input")

            progress.publish(mid, "normalize", 10, "Normalising audio to 16 kHz mono")
            wav = to_wav16k_mono(raw, tmpdir / "audio.wav")
            meeting.duration_seconds = duration_seconds(wav)
            db.commit()

            # [2] Hosted ASR ----------------------------------------------
            provider = get_provider(meeting.asr_provider)
            progress.publish(mid, "transcribe", 20, f"Transcribing via {provider.name}")
            result = provider.transcribe(wav, language_hint=meeting.language_hint)
            if not result.segments:
                raise RuntimeError("ASR returned an empty transcript")
            meeting.asr_provider = provider.name
            db.commit()
            progress.publish(
                mid,
                "transcribe",
                55,
                f"{len(result.segments)} segments, {len(result.speaker_labels)} speakers",
            )

            # [3] Speaker identification (optional) ------------------------
            # Off by default: get the transcript right first. Turn on with
            # AUTO_IDENTIFY_SPEAKERS once people have enrolled voice samples.
            if settings.auto_identify_speakers:
                progress.publish(mid, "identify", 60, "Matching voices against enrolled users")
                enrolled = load_enrolled_voices(db)
                resolutions = identify_speakers(wav, result.segments, enrolled)
                named = sum(1 for r in resolutions if r.user_id)
                progress.publish(
                    mid, "identify", 75, f"Identified {named} of {len(resolutions)} speakers"
                )
            else:
                resolutions = label_speakers_only(result.segments)
                progress.publish(
                    mid, "identify", 75, f"{len(resolutions)} speakers separated"
                )

            # [4] Persist --------------------------------------------------
            _replace_transcript(db, meeting, result, resolutions)
            meeting.status = MeetingStatus.transcribed
            meeting.processed_at = datetime.now(timezone.utc)
            db.commit()

            # [5] Minutes (optional) ---------------------------------------
            # Also off by default. Minutes stay available on demand from the
            # meeting page, so nothing is lost by leaving this off - you just
            # do not pay for them on every meeting while validating transcripts.
            if settings.auto_generate_minutes:
                progress.publish(mid, "minutes", 85, "Generating minutes")
                generate_and_store_minutes(db, meeting)
                meeting.status = MeetingStatus.completed
                db.commit()

            progress.publish(mid, "done", 100, "Transcript ready")

    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then re-raised
        log.exception("Pipeline failed for meeting %s", mid)
        meeting.status = MeetingStatus.failed
        meeting.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        progress.publish(mid, "failed", 100, meeting.error)
        raise


def _replace_transcript(db: Session, meeting: Meeting, result, resolutions) -> None:
    """Write segments and participants, preserving any manual speaker corrections."""
    manual = {
        p.speaker_label: p
        for p in db.execute(
            select(Participant).where(
                Participant.meeting_id == meeting.id, Participant.is_manual.is_(True)
            )
        ).scalars()
    }

    for existing in list(meeting.segments):
        db.delete(existing)
    for existing in list(meeting.participants):
        if existing.speaker_label not in manual:
            db.delete(existing)
    db.flush()

    for resolution in resolutions:
        if resolution.speaker_label in manual:
            # A human already told us who this is - do not overwrite them.
            manual[resolution.speaker_label].speaking_seconds = resolution.speaking_seconds
            continue
        db.add(
            Participant(
                meeting_id=meeting.id,
                speaker_label=resolution.speaker_label,
                user_id=uuid.UUID(resolution.user_id) if resolution.user_id else None,
                display_name=resolution.display_name,
                confidence=resolution.confidence,
                speaking_seconds=resolution.speaking_seconds,
            )
        )

    for idx, seg in enumerate(result.segments):
        profile = analyse(seg.text)
        # For a monolingual segment the script is a fact and the ASR's tag is a
        # guess, so the script wins. For a code-switched one, letter counts are
        # a bad proxy for the matrix language - English words simply use more
        # letters than Devanagari conjuncts - so defer to the ASR there.
        language = (
            (seg.language or profile.language)
            if profile.is_mixed
            else (profile.language or seg.language)
        )
        db.add(
            Segment(
                meeting_id=meeting.id,
                idx=idx,
                start_ms=int(seg.start * 1000),
                end_ms=int(seg.end * 1000),
                speaker_label=seg.speaker,
                language=language or result.language,
                scripts=profile.label,
                text=seg.text,
                confidence=seg.confidence,
            )
        )
    db.flush()


def generate_and_store_minutes(
    db: Session, meeting: Meeting, output_language: str | None = None
) -> None:
    names = {p.speaker_label: p.display_name for p in meeting.participants}
    segments = [
        {
            "start_ms": s.start_ms,
            "speaker": names.get(s.speaker_label, s.speaker_label),
            "text": s.text,
            "language": s.language,
        }
        for s in sorted(meeting.segments, key=lambda s: s.idx)
    ]

    generated = generate_minutes(
        title=meeting.title, segments=segments, output_language=output_language
    )

    existing = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting.id)
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)
        db.flush()

    db.add(
        Minutes(
            meeting_id=meeting.id,
            summary=generated.summary,
            decisions=[d.model_dump() for d in generated.decisions],
            action_items=[a.model_dump() for a in generated.action_items],
            topics=[t.model_dump() for t in generated.topics],
            open_questions=generated.open_questions,
            languages_detected=generated.languages_detected,
            model=settings.anthropic_model,
        )
    )
    db.flush()
