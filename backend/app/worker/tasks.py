from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app import features, storage
from app.audio import to_wav16k_mono
from app.db import SessionLocal
from app.models import Meeting, Minutes, MinutesVersion, Segment, Voiceprint
from app.pipeline import process_meeting
from app.speakers.identify import embed_cluster
from app.worker.celery_app import celery

log = logging.getLogger(__name__)


@celery.task(name="meetings.process", bind=True, max_retries=0)
def process_meeting_task(self, meeting_id: str) -> dict:
    """Run the full pipeline for one meeting.

    Deliberately no auto-retry: a failed run has already written the error onto
    the meeting row for the user to see, and blindly re-running a hosted-ASR
    call costs real money. Re-queue explicitly via POST /reprocess instead.
    """
    db = SessionLocal()
    try:
        process_meeting(db, uuid.UUID(meeting_id))
        return {"meeting_id": meeting_id, "status": "completed"}
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
