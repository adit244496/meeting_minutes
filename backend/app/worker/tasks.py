from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app import features, progress, storage
from app.asr.base import ProviderBusyError
from app.audio import to_wav16k_mono
from app.db import SessionLocal
from app.models import Meeting, MeetingStatus, Minutes, MinutesVersion, Segment, Voiceprint
from app.pipeline import generate_and_store_minutes, process_meeting
from app.speakers.identify import embed_cluster
from app.worker.celery_app import celery

log = logging.getLogger(__name__)

# Import the Gemini SDK here, in the prefork parent, so every child process
# inherits it. Imported lazily inside the provider instead, it cost ~6s at the
# start of the first meeting each child handled.
try:
    import google.genai  # noqa: F401
except ImportError:  # pragma: no cover - only needed for the Gemini provider
    pass


# Waits before re-running a meeting whose provider was overloaded. The
# provider already backed off for about a minute inside the run.
BUSY_RETRY_DELAYS_SECONDS = (120, 300, 600)


@celery.task(name="meetings.process", bind=True, max_retries=len(BUSY_RETRY_DELAYS_SECONDS))
def process_meeting_task(self, meeting_id: str) -> dict:
    """Run the full pipeline for one meeting.

    Retries only when the provider was overloaded (ProviderBusyError): a busy
    response is not billed and nothing is wrong with the meeting. Any other
    failure has already written the error onto the meeting row, and blindly
    re-running a hosted-ASR call costs real money - re-queue explicitly via
    POST /reprocess instead.
    """
    attempt = self.request.retries
    retry_in = (
        BUSY_RETRY_DELAYS_SECONDS[attempt] if attempt < len(BUSY_RETRY_DELAYS_SECONDS) else None
    )
    db = SessionLocal()
    try:
        process_meeting(db, uuid.UUID(meeting_id), retry_in_seconds=retry_in)
        return {"meeting_id": meeting_id, "status": "completed"}
    except ProviderBusyError as exc:
        if retry_in is None:
            raise
        raise self.retry(exc=exc, countdown=retry_in)
    finally:
        db.close()


@celery.task(name="live.transcribe", bind=True, max_retries=0)
def live_transcribe_task(self, meeting_id: str) -> dict:
    """One pass of the live transcript: transcribe audio that arrived since the last.

    Queued by the API as audio pieces arrive (at most one at a time per meeting).
    Goes again straight away while a backlog remains, e.g. after a slow model call.
    """
    from app import live

    db = SessionLocal()
    try:
        result = live.transcribe_pending(db, uuid.UUID(meeting_id))
    except Exception:  # noqa: BLE001 - a failed preview must not break the recording
        log.exception("Live transcription pass failed for meeting %s", meeting_id)
        progress.publish(meeting_id, "live", 0, "Live transcript hit an error — it will retry on the next audio")
        result = {"error": True}
    finally:
        db.close()
    if result.get("more") and live.try_queue(meeting_id):
        live_transcribe_task.delay(meeting_id)
    return result


@celery.task(name="transcripts.translate", bind=True, max_retries=0)
def translate_transcript_task(self, meeting_id: str, language: str) -> dict:
    """Translate a meeting's transcript into one language, in the background.

    Progress goes out on the meeting's channel under the "translate" stage and
    ends with "translate_done" or "translate_failed", so the page can follow it
    the way it follows transcription and minutes.
    """
    from app import transcripts

    db = SessionLocal()
    try:
        meeting = db.get(Meeting, uuid.UUID(meeting_id))
        if meeting is None:
            return {"translated": False, "reason": "meeting not found"}

        transcripts.translate(
            db,
            meeting,
            language,
            # The language rides along, so a page that opens mid-translation
            # knows which one is being made, not just that something is.
            on_progress=lambda fraction, message: progress.publish(
                meeting_id,
                "translate",
                max(2, min(99, round(fraction * 100))),
                message,
                language=language,
            ),
        )
        progress.publish(
            meeting_id, "translate_done", 100, f"{language} transcript ready", language=language
        )
        return {"translated": True}
    except Exception as exc:  # noqa: BLE001 - reported to the page, not retried
        log.exception("Transcript translation failed for meeting %s", meeting_id)
        db.rollback()
        progress.publish(meeting_id, "translate_failed", 100, str(exc), language=language)
        return {"translated": False, "reason": str(exc)}
    finally:
        db.close()


@celery.task(name="minutes.generate", bind=True, max_retries=0)
def generate_minutes_task(
    self,
    meeting_id: str,
    language: str | None = None,
    kind: str = "detailed",
    model: str | None = None,
) -> dict:
    """Generate (or regenerate) minutes for a meeting that has a transcript.

    Runs in the worker rather than the request: an LLM writing minutes for a
    long meeting takes a minute or more, which would otherwise hold an HTTP
    request open with nothing to show. Progress goes out on the meeting's
    progress channel under the "minutes" stage, finishing with "minutes_done"
    or "minutes_failed" - distinct from the transcription pipeline's "done"
    and "failed", which change the meeting's status.
    """
    mid = meeting_id
    db = SessionLocal()
    try:
        meeting = db.get(Meeting, uuid.UUID(meeting_id))
        if meeting is None:
            return {"generated": False, "reason": "meeting not found"}

        progress.publish(mid, "minutes", 3, "Loading the transcript")
        generate_and_store_minutes(
            db,
            meeting,
            output_language=language,
            kind=kind,
            model_override=model,
            on_progress=lambda fraction, message: progress.publish(
                mid, "minutes", max(3, min(99, round(fraction * 100))), message
            ),
        )
        meeting.status = MeetingStatus.completed
        meeting.error = None
        db.commit()
        progress.publish(mid, "minutes_done", 100, f"{kind.capitalize()} minutes ready")
        return {"generated": True}
    except Exception as exc:  # noqa: BLE001 - reported to the page, not retried
        log.exception("Minutes generation failed for meeting %s", mid)
        db.rollback()
        progress.publish(mid, "minutes_failed", 100, str(exc))
        return {"generated": False, "reason": str(exc)}
    finally:
        db.close()


@celery.task(name="voiceprints.harvest", bind=True, max_retries=0)
def harvest_voiceprint_task(self, meeting_id: str, speaker_label: str, user_id: str) -> dict:
    """Turn a human speaker correction into a new enrollment sample.

    When somebody tells us "Unknown Speaker 2 was Rahul", we already have a
    diarized, verified block of Rahul's speech sitting in this meeting. Embedding
    it costs nothing and makes the next meeting recognise him automatically.
    Runs in the worker because it re-downloads the full meeting audio.
    """
    from app.asr.base import TranscriptSegment

    db = SessionLocal()
    try:
        meeting = db.get(Meeting, uuid.UUID(meeting_id))
        if meeting is None or not meeting.audio_key:
            return {"enrolled": False, "reason": "no audio"}

        rows = db.execute(
            select(Segment)
            .where(Segment.meeting_id == meeting.id, Segment.speaker_label == speaker_label)
            .order_by(Segment.idx)
        ).scalars().all()
        if not rows:
            return {"enrolled": False, "reason": "no segments"}

        spans = [
            TranscriptSegment(
                start=r.start_ms / 1000.0,
                end=r.end_ms / 1000.0,
                speaker=r.speaker_label,
                text=r.text,
            )
            for r in rows
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            raw = storage.download_to(meeting.audio_key, tmpdir / "raw_input")
            wav = to_wav16k_mono(raw, tmpdir / "audio.wav")

            embedding = embed_cluster(wav, spans)
            if embedding is None:
                return {"enrolled": False, "reason": "not enough clean speech"}

            total = sum(s.end - s.start for s in spans)

        # No new object is written: the source audio already lives in storage
        # and the exact spans are recoverable from `segments`, so point at that
        # rather than duplicating audio we already pay to keep.
        db.add(
            Voiceprint(
                user_id=uuid.UUID(user_id),
                embedding=embedding.tolist(),
                sample_key=meeting.audio_key,
                duration_seconds=round(total, 2),
                origin="correction",
            )
        )
        db.commit()
        log.info("Harvested voiceprint for user %s from meeting %s", user_id, meeting_id)
        return {"enrolled": True, "seconds": round(total, 2)}
    finally:
        db.close()


@celery.task(name="recordings.purge", bind=True, max_retries=0)
def purge_old_recordings(self) -> dict:
    """Apply the retention policy to each tier of meeting data.

    Three independent windows, administered from Users & settings rather than
    the environment, because retention is a policy decision somebody makes once
    and then changes without a deploy:

      recordings  - the audio file. Largest artifact, shortest default (7 days).
      transcripts - the segment rows. Small, medium default (30 days).
      minutes     - the minutes and their edit history. Default: forever.

    0 means keep forever for any tier. The meeting row itself is never deleted:
    what is gone is stamped, so the UI can explain an empty tab rather than
    presenting a meeting that looks broken. Enrollment voiceprints live under a
    different key prefix and are never touched here.
    """
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        windows = features.all_numbers(db)
        result: dict = {"retention_days": windows}

        # --- recordings -------------------------------------------------
        days = windows["retention_days_recordings"]
        freed = 0
        stale = (
            db.execute(
                select(Meeting).where(
                    Meeting.audio_key.is_not(None),
                    Meeting.started_at < now - timedelta(days=days),
                )
            ).scalars().all()
            if days > 0
            else []
        )
        for meeting in stale:
            freed += storage.size_bytes(meeting.audio_key) or 0
            storage.delete(meeting.audio_key)
            meeting.audio_key = None
            meeting.audio_deleted_at = now
        result["recordings_deleted"] = len(stale)
        result["freed_bytes"] = freed

        # --- transcripts ------------------------------------------------
        days = windows["retention_days_transcripts"]
        cleared = 0
        if days > 0:
            cutoff = now - timedelta(days=days)
            meetings = db.execute(
                select(Meeting).where(
                    Meeting.started_at < cutoff,
                    Meeting.transcript_deleted_at.is_(None),
                )
            ).scalars().all()
            for meeting in meetings:
                rows = db.execute(
                    select(Segment).where(Segment.meeting_id == meeting.id)
                ).scalars().all()
                if not rows:
                    continue
                for row in rows:
                    db.delete(row)
                meeting.transcript_deleted_at = now
                cleared += 1
        result["transcripts_deleted"] = cleared

        # --- minutes ----------------------------------------------------
        days = windows["retention_days_minutes"]
        removed = 0
        if days > 0:
            cutoff = now - timedelta(days=days)
            meetings = db.execute(
                select(Meeting).where(
                    Meeting.started_at < cutoff,
                    Meeting.minutes_deleted_at.is_(None),
                )
            ).scalars().all()
            for meeting in meetings:
                rows = db.execute(
                    select(Minutes).where(Minutes.meeting_id == meeting.id)
                ).scalars().all()
                history = db.execute(
                    select(MinutesVersion).where(MinutesVersion.meeting_id == meeting.id)
                ).scalars().all()
                if not rows and not history:
                    continue
                for row in list(rows) + list(history):
                    db.delete(row)
                meeting.minutes_deleted_at = now
                removed += 1
        result["minutes_deleted"] = removed

        db.commit()
        if stale or cleared or removed:
            log.info(
                "Retention sweep: %d recording(s) (~%.1f MB), %d transcript(s), "
                "%d set(s) of minutes",
                len(stale), freed / 1_000_000, cleared, removed,
            )
        return result
    finally:
        db.close()
