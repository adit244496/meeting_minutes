"""Near-real-time transcription of a meeting that is still being recorded.

The browser's MediaRecorder emits the recording as a stream of blobs. Appended
in order, those blobs are a valid, growing audio file, and ffmpeg decodes a
file that is still growing up to its last complete frame. So the recording is
kept as one buffer file, and every minute or so the worker transcribes the
stretch after the last transcribed point and appends it to the transcript.

This is a preview, not the record. Each stretch is transcribed on its own, so
speaker ids can drift between stretches and a word can be split at a boundary.
When recording stops, the complete file goes through the normal pipeline, whose
transcript replaces the live one entirely.

The buffer lives on local disk (under LOCAL_STORAGE_DIR, shared by the API and
the worker) whatever the storage backend: it is appended to every few seconds,
which object storage cannot do, and it is deleted when recording stops.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import progress
from app.config import settings
from app.models import Meeting, Segment

log = logging.getLogger(__name__)

# Transcribe once this much new audio has arrived.
MIN_WINDOW_SECONDS = 45
# Never send more than this in one go, so a backlog catches up in steps and
# each step returns quickly.
MAX_WINDOW_SECONDS = 240
# Leave the very end out: the last blob may end mid-frame.
TAIL_GUARD_SECONDS = 1.0
# Lines of earlier transcript given to the model as context.
CONTEXT_LINES = 6

_redis = None


def _r():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.redis_url)
    return _redis


def buffer_path(meeting_id: str, suffix: str = "webm") -> Path:
    root = Path(settings.local_storage_dir) / "live"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{meeting_id}.{suffix}"


def find_buffer(meeting_id: str) -> Path | None:
    for suffix in ("webm", "mp4", "ogg"):
        path = buffer_path(meeting_id, suffix)
        if path.exists():
            return path
    return None


def _seq_key(meeting_id: str) -> str:
    return f"live:{meeting_id}:next_seq"


def start(meeting_id: str) -> None:
    for old in ("webm", "mp4", "ogg"):
        buffer_path(meeting_id, old).unlink(missing_ok=True)
    _r().set(_seq_key(meeting_id), 0, ex=24 * 3600)


def append_chunk(meeting_id: str, seq: int, data: bytes, suffix: str) -> int:
    """Append one blob in order. Returns the next sequence number expected.

    A repeat of an already-stored blob (a retry after a lost response) is
    ignored; a gap raises, so the client resends from the expected number
    rather than the buffer silently missing a stretch of audio.
    """
    expected = int(_r().get(_seq_key(meeting_id)) or 0)
    if seq < expected:
        return expected
    if seq > expected:
        raise LookupError(expected)
    with buffer_path(meeting_id, suffix).open("ab") as fh:
        fh.write(data)
    _r().set(_seq_key(meeting_id), expected + 1, ex=24 * 3600)
    return expected + 1


def stop(meeting_id: str) -> None:
    buffer = find_buffer(meeting_id)
    if buffer:
        buffer.unlink(missing_ok=True)
    _r().delete(_seq_key(meeting_id))


def try_queue(meeting_id: str) -> bool:
    """True if the caller should queue a transcription pass now.

    At most one pass is queued or running per meeting; chunks arriving in the
    meantime are picked up by the next pass.
    """
    return bool(_r().set(f"live:{meeting_id}:queued", 1, nx=True, ex=600))


def _release(meeting_id: str) -> None:
    _r().delete(f"live:{meeting_id}:queued")


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}" if total < 3600 else f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def transcribe_pending(db: Session, meeting_id: uuid.UUID) -> dict:
    """Transcribe the untranscribed stretch of a live recording, if long enough.

    Returns {"more": True} when a backlog remains, so the task can go again.
    """
    from app.asr import get_provider
    from app.asr.base import ProviderBusyError
    from app.audio import duration_seconds, extract_clip, to_wav16k_mono
    from app.pipeline import segment_row

    mid = str(meeting_id)
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None or not meeting.is_live:
            return {"skipped": "not live"}
        buffer = find_buffer(mid)
        if buffer is None:
            return {"skipped": "no audio yet"}

        until = float(meeting.live_transcribed_until or 0.0)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Snapshot first: the API keeps appending to the buffer meanwhile.
            snapshot = tmpdir / f"snapshot{buffer.suffix}"
            snapshot.write_bytes(buffer.read_bytes())
            try:
                wav = to_wav16k_mono(snapshot, tmpdir / "live.wav")
            except Exception:  # noqa: BLE001 - a buffer this short may not decode yet
                return {"skipped": "not decodable yet"}
            total = duration_seconds(wav)

            available = total - TAIL_GUARD_SECONDS - until
            if available < MIN_WINDOW_SECONDS:
                return {"skipped": f"{available:.0f}s new audio"}
            end = until + min(available, MAX_WINDOW_SECONDS)
            clip = extract_clip(wav, tmpdir / "clip.wav", until, end)

            recent = db.execute(
                select(Segment)
                .where(Segment.meeting_id == meeting_id)
                .order_by(Segment.idx.desc())
                .limit(CONTEXT_LINES)
            ).scalars().all()
            # Speaker numbers as the model writes them in its output format.
            context = "\n".join(
                f"speaker {int(''.join(ch for ch in s.speaker_label if ch.isdigit()) or 0)}: {s.text}"
                for s in reversed(recent)
            ) or "(nothing yet - this is the start)"

            provider = get_provider(meeting.asr_provider, db=db)
            language_hint = meeting.language_hint
            # No locks held during the ASR call.
            db.commit()
            progress.publish(mid, "live", 0, f"Transcribing {_clock(until)}–{_clock(end)}…")
            try:
                if provider.name == "gemini":
                    result = provider.transcribe(clip, language_hint=language_hint, context=context)
                else:
                    result = provider.transcribe(clip, language_hint=language_hint)
            except ProviderBusyError:
                progress.publish(mid, "live", 0, "Live transcript paused — the transcription service is busy")
                return {"skipped": "provider busy"}

        # Recording may have stopped while the model was working; the final
        # pass owns the transcript from then on.
        meeting = db.execute(
            select(Meeting).where(Meeting.id == meeting_id).with_for_update()
        ).scalar_one_or_none()
        if meeting is None or not meeting.is_live:
            db.rollback()
            return {"skipped": "recording stopped"}

        next_idx = db.execute(
            select(func.coalesce(func.max(Segment.idx), -1)).where(Segment.meeting_id == meeting_id)
        ).scalar_one() + 1
        for offset, seg in enumerate(result.segments):
            db.add(segment_row(meeting_id, next_idx + offset, seg, result.language, offset_s=until))
        meeting.live_transcribed_until = end
        db.commit()

        progress.publish(mid, "live", 0, f"Live transcript up to {_clock(end)}")
        log.info("Live meeting %s: transcribed %.0f-%.0fs (%d segments)", mid, until, end, len(result.segments))
        return {"transcribed_until": end, "more": total - TAIL_GUARD_SECONDS - end >= MIN_WINDOW_SECONDS}
    finally:
        _release(mid)
