from __future__ import annotations

import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app import storage
from app.audio import to_wav16k_mono
from app.config import settings
from app.db import SessionLocal
from app.models import Meeting, Segment, Voiceprint
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
def purge_old_recordings(self, retention_days: int | None = None) -> dict:
    """Delete meeting audio past its retention window.

    Deliberately narrow: this removes the audio file and stamps
    `audio_deleted_at`. The meeting row, its transcript segments, its speakers
    and its minutes are all left alone - those are the record of what happened
    and they are kept indefinitely. Enrollment voiceprints are also untouched;
    they live under a different key prefix and are needed for future matching.
    """
    days = retention_days if retention_days is not None else settings.recording_retention_days
    if days <= 0:
        return {"skipped": "retention disabled"}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        stale = db.execute(
            select(Meeting).where(
                Meeting.audio_key.is_not(None),
                Meeting.started_at < cutoff,
            )
        ).scalars().all()

        deleted, freed = 0, 0
        for meeting in stale:
            freed += storage.size_bytes(meeting.audio_key) or 0
            storage.delete(meeting.audio_key)
            meeting.audio_key = None
            meeting.audio_deleted_at = datetime.now(timezone.utc)
            deleted += 1

        db.commit()
        if deleted:
            log.info(
                "Purged %d recording(s) older than %d days, freed ~%.1f MB",
                deleted, days, freed / 1_000_000,
            )
        return {"deleted": deleted, "freed_bytes": freed, "retention_days": days}
    finally:
        db.close()
