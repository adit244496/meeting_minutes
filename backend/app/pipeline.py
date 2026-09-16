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
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import credentials, features, progress, storage, transcripts
from app.asr import get_provider
from app.asr.base import ProviderBusyError
from app.audio import duration_seconds, to_wav16k_mono
from app.config import settings
from app.lang import analyse
from app.minutes import catalog
from app.minutes import versions as versions_store
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


def process_meeting(
    db: Session,
    meeting_id: uuid.UUID,
    retry_in_seconds: int | None = None,
) -> None:
    """Run the pipeline for one meeting.

    `retry_in_seconds` is set by the task when it will retry after a busy
    provider. In that case a ProviderBusyError leaves the meeting processing
    and says when the next attempt is, instead of marking it failed.
    """
    meeting = db.get(Meeting, meeting_id)
    if meeting is None:
        raise ValueError(f"meeting {meeting_id} not found")
    if not meeting.audio_key:
        raise ValueError(f"meeting {meeting_id} has no audio")

    mid = str(meeting_id)
    meeting.status = MeetingStatus.processing
    meeting.error = None
    db.commit()

    # Percent bands per stage. Transcription dominates the wall-clock time, so
    # it gets most of the bar.
    started = time.monotonic()
    mark = started

    def timed(stage: str) -> None:
        nonlocal mark
        now = time.monotonic()
        log.info("Meeting %s: %s took %.1fs", mid, stage, now - mark)
        mark = now

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # [1] Fetch and normalise -------------------------------------
            progress.publish(mid, "prepare", 2, "Fetching audio")
            raw = storage.download_to(meeting.audio_key, tmpdir / "raw_input")

            progress.publish(mid, "prepare", 5, "Preparing audio")
            wav = to_wav16k_mono(raw, tmpdir / "audio.wav")
            meeting.duration_seconds = duration_seconds(wav)
            db.commit()
            timed("prepare")

            # [2] Hosted ASR ----------------------------------------------
            provider = get_provider(meeting.asr_provider, db=db)
            progress.publish(mid, "transcribe", 8, f"Transcribing with {provider.name}")

            def on_transcribe_progress(fraction: float, message: str) -> None:
                progress.publish(mid, "transcribe", 8 + round(80 * max(0.0, min(fraction, 1.0))), message)

            # End the transaction get_provider opened when it read the settings.
            # Otherwise its locks are held for the whole ASR call - minutes - and
            # any schema change or settings write in that window deadlocks.
            db.commit()
            result = provider.transcribe(
                wav, language_hint=meeting.language_hint, on_progress=on_transcribe_progress
            )
            if not result.segments:
                raise RuntimeError("ASR returned an empty transcript")
            meeting.asr_provider = provider.name
            db.commit()
            timed("transcribe")
            progress.publish(
                mid,
                "speakers",
                88,
                f"{len(result.segments)} segments, {len(result.speaker_labels)} speakers",
            )

            # [3] Speaker identification (optional) ------------------------
            # Off by default: get the transcript right first. Turn on with
            # AUTO_IDENTIFY_SPEAKERS once people have enrolled voice samples.
            # An admin can turn matching on from Settings without a redeploy;
            # AUTO_IDENTIFY_SPEAKERS in the environment still forces it on.
            if settings.auto_identify_speakers or features.is_enabled(db, "speaker_matching_enabled"):
                progress.publish(mid, "speakers", 89, "Matching voices against enrolled users")
                enrolled = load_enrolled_voices(db)
                resolutions = identify_speakers(wav, result.segments, enrolled)
                named = sum(1 for r in resolutions if r.user_id)
                progress.publish(
                    mid, "speakers", 91, f"Identified {named} of {len(resolutions)} speakers"
                )
            else:
                resolutions = label_speakers_only(result.segments)

            # [4] Persist --------------------------------------------------
            progress.publish(mid, "speakers", 92, "Saving transcript")
            _replace_transcript(db, meeting, result, resolutions)
            meeting.status = MeetingStatus.transcribed
            meeting.processed_at = datetime.now(timezone.utc)
            db.commit()
            timed("speakers and save")

            # [5] Minutes (optional) ---------------------------------------
            # Also off by default. Minutes stay available on demand from the
            # meeting page, so nothing is lost by leaving this off - you just
            # do not pay for them on every meeting while validating transcripts.
            done_message = "Transcript ready"
            if settings.auto_generate_minutes:
                # Only the short version is automatic. Detailed minutes take
                # longer and cost more, so they are generated on request.
                progress.publish(mid, "minutes", 93, "Writing the short minutes")
                try:
                    generate_and_store_minutes(
                        db,
                        meeting,
                        kind="short",
                        on_progress=lambda fraction, message: progress.publish(
                            mid, "minutes", 93 + round(6 * fraction), message
                        ),
                    )
                    meeting.status = MeetingStatus.completed
                    done_message = "Short minutes ready"
                except Exception as exc:  # noqa: BLE001
                    # The transcript is already saved and is the primary record.
                    # A missing key or an LLM outage must not mark the whole
                    # meeting failed - minutes can be generated from the page.
                    log.exception("Minutes failed for meeting %s", mid)
                    db.rollback()
                    meeting.error = f"Minutes could not be generated: {exc}"
                    done_message = "Transcript ready - minutes could not be generated"
                db.commit()
                timed("minutes")

            log.info("Meeting %s processed in %.1fs", mid, time.monotonic() - started)
            progress.publish(mid, "done", 100, done_message)

    except ProviderBusyError as exc:
        if retry_in_seconds is None:
            _mark_failed(db, meeting, mid, exc)
            raise
        # Not a failure yet: the task will run the meeting again later.
        log.warning("Meeting %s: provider busy, retrying in %ss", mid, retry_in_seconds)
        meeting.status = MeetingStatus.processing
        meeting.error = None
        db.commit()
        at = (datetime.now() + timedelta(seconds=retry_in_seconds)).strftime("%H:%M")
        progress.publish(
            mid, "waiting", 8,
            f"The transcription service is overloaded. Trying again automatically in "
            f"{max(1, retry_in_seconds // 60)} min (around {at}).",
        )
        raise

    except Exception as exc:  # noqa: BLE001 - surfaced to the user, then re-raised
        log.exception("Pipeline failed for meeting %s", mid)
        _mark_failed(db, meeting, mid, exc)
        raise


def _mark_failed(db: Session, meeting: Meeting, mid: str, exc: Exception) -> None:
    db.rollback()
    meeting.status = MeetingStatus.failed
    meeting.error = f"{type(exc).__name__}: {exc}"
    db.commit()
    progress.publish(mid, "failed", 100, meeting.error)


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

    # Query rather than iterating meeting.segments / meeting.participants: those
    # collections were loaded before this pipeline inserted anything and the
    # session does not expire on commit, so on a reprocess they can be empty
    # while rows exist - leaving the old transcript behind alongside the new one.
    for existing in db.execute(
        select(Segment).where(Segment.meeting_id == meeting.id)
    ).scalars():
        db.delete(existing)
    for existing in db.execute(
        select(Participant).where(Participant.meeting_id == meeting.id)
    ).scalars():
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
        db.add(segment_row(meeting.id, idx, seg, result.language))
    # Translations were made from the transcript that has just been replaced.
    transcripts.clear(db, meeting.id)
    db.flush()


def segment_row(meeting_id: uuid.UUID, idx: int, seg, fallback_language: str | None, offset_s: float = 0.0) -> Segment:
    """One ASR segment as a database row. `offset_s` shifts a live chunk's
    timestamps to their place in the whole recording."""
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
    return Segment(
        meeting_id=meeting_id,
        idx=idx,
        start_ms=int((seg.start + offset_s) * 1000),
        end_ms=int((seg.end + offset_s) * 1000),
        speaker_label=seg.speaker,
        language=language or fallback_language,
        scripts=profile.label,
        text=seg.text,
        confidence=seg.confidence,
    )


def previous_meeting(db: Session, meeting: Meeting) -> Meeting | None:
    """The latest earlier meeting in the same series that has minutes.

    Restricted to the same department. A series can span departments - an
    administrator can file any meeting into any series - and follow-ups quote
    the previous meeting's action items verbatim, which would copy one
    department's minutes into another's.
    """
    if not meeting.series_id:
        return None
    return db.execute(
        select(Meeting)
        .where(
            Meeting.series_id == meeting.series_id,
            Meeting.id != meeting.id,
            Meeting.started_at < meeting.started_at,
            Meeting.department_id.is_not_distinct_from(meeting.department_id),
            Meeting.id.in_(select(Minutes.meeting_id)),
        )
        .order_by(Meeting.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def previous_meeting_context(db: Session, meeting: Meeting) -> dict | None:
    """What the minutes writer needs to know about the previous meeting.

    Detailed minutes are preferred when they exist - they list every action
    item, where the short version keeps only the main ones.
    """
    prior = previous_meeting(db, meeting)
    if prior is None:
        return None
    rows = {
        m.kind: m
        for m in db.execute(select(Minutes).where(Minutes.meeting_id == prior.id)).scalars()
    }
    source = rows.get("detailed") or rows.get("short")
    if source is None:
        return None
    return {
        "title": prior.title,
        "date": prior.started_at.strftime("%d %b %Y") if prior.started_at else "",
        "summary": source.summary,
        "decisions": source.decisions,
        "action_items": source.action_items,
        "open_questions": source.open_questions,
    }


def generate_and_store_minutes(
    db: Session,
    meeting: Meeting,
    output_language: str | None = None,
    on_progress=None,
    kind: str = "detailed",
    model_override: str | None = None,
) -> None:
    # Query the rows rather than reading meeting.segments / meeting.participants.
    # The pipeline inserts them with db.add(Segment(meeting_id=...)) instead of
    # appending to the relationship, and the session is expire_on_commit=False,
    # so those collections stay as they were first loaded - empty. Reading them
    # here produced "cannot generate minutes from an empty transcript" for a
    # meeting whose transcript had saved perfectly well.
    participants = db.execute(
        select(Participant).where(Participant.meeting_id == meeting.id)
    ).scalars().all()
    rows = db.execute(
        select(Segment).where(Segment.meeting_id == meeting.id).order_by(Segment.idx)
    ).scalars().all()

    names = {p.speaker_label: p.display_name for p in participants}
    segments = [
        {
            "start_ms": s.start_ms,
            "speaker": names.get(s.speaker_label, s.speaker_label),
            "text": s.text,
            "language": s.language,
        }
        for s in rows
    ]

    # Resolved here rather than inside generate_minutes: that module has no
    # session, and the admin panel has to win over the environment.
    # A model picked on the meeting page decides the provider; otherwise the
    # admin's minutes provider and its model apply.
    override_provider = catalog.provider_of(model_override) if model_override else None
    provider = override_provider or (credentials.resolve(db, "minutes_provider") or "anthropic").lower()
    if provider == "openai":
        model = model_override if override_provider else (credentials.resolve(db, "openai_model") or settings.openai_model)
        api_key = credentials.resolve(db, "openai_api_key")
    else:
        model = model_override if override_provider else (credentials.resolve(db, "anthropic_model") or settings.anthropic_model)
        api_key = credentials.resolve(db, "anthropic_api_key")
    previous = previous_meeting_context(db, meeting)
    # Release locks before the LLM call, for the same reason as before ASR.
    db.commit()
    generated = generate_minutes(
        title=meeting.title,
        segments=segments,
        output_language=output_language,
        model=model,
        api_key=api_key or None,
        provider=provider,
        on_progress=on_progress,
        kind=kind,
        previous=previous,
    )

    existing = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting.id, Minutes.kind == kind)
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)
        db.flush()

    # The version number continues the meeting's history rather than restarting
    # at 1: regenerating must never look like it erased someone's edit, and the
    # edit itself is still in minutes_versions and can be restored.
    minutes = Minutes(
        meeting_id=meeting.id,
        kind=kind,
        summary=generated.summary,
        key_points=generated.key_points,
        follow_ups=[f.model_dump() for f in generated.follow_ups] if previous else [],
        decisions=[d.model_dump() for d in generated.decisions],
        action_items=[a.model_dump() for a in generated.action_items],
        topics=[t.model_dump() for t in generated.topics],
        open_questions=generated.open_questions,
        languages_detected=generated.languages_detected,
        model=model,
        version=versions_store.next_version(db, meeting.id, kind),
        source="generated",
    )
    db.add(minutes)
    db.flush()
    versions_store.record(db, meeting.id, minutes, source="generated")
