"""Translating a transcript, without touching the transcript.

The record of a meeting is what was said, in the language it was said in:
Bengali stays in Bengali script, an English technical term stays in English,
and a sentence that switches halfway keeps both halves. That is what
`segments` holds, and nothing here changes it.

A translation is a separate, cached rendering of that transcript for someone
who does not read one of the languages. It is generated on request, stored per
language, and thrown away when the transcript is regenerated.

Lines go to the model as "<index>|<text>" and come back the same way. The
index is what makes it safe: a reply can be matched to the segment it belongs
to, and a line the model drops or merges is detected rather than silently
shifting every later line onto the wrong speaker.
"""

from __future__ import annotations

import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import credentials
from app.config import settings
from app.models import Meeting, Segment, TranscriptTranslation

log = logging.getLogger(__name__)

LANGUAGES = {"en": "English", "bn": "Bengali", "hi": "Hindi"}

# Translated in batches: one call per batch keeps each response short enough to
# stay reliable, and lets progress move while a long meeting is worked through.
#
# The batches run at the same time, which is what makes this quick. Translation
# time is nearly all waiting on the model, and a batch does not depend on any
# other - each line carries its own number - so running them one after another
# just added up the waits. A 228-line meeting went from ~32s to well under 15s.
BATCH_SIZE = 40
# Enough to hide the latency without tripping a provider's rate limit.
MAX_PARALLEL = 4

_LINE = re.compile(r"^\s*(\d+)\s*\|\s*(.*\S)\s*$")


class TranslationCancelled(RuntimeError):
    """Somebody stopped this translation. Not a failure - nothing is stored."""

SYSTEM_PROMPT = """You translate meeting transcripts.

- Translate every line into {language}, and write it in {language}'s own script.
- Keep the meaning and the register: this is speech, not prose. Keep false \
starts and filler if they carry meaning; do not tidy the speaker up.
- Keep names, product names, technical terms, abbreviations and numbers as they \
are. Do not translate BOQ, SAP, TAT and the like, and never transliterate a \
person's name.
- A line already in {language} is returned unchanged.
- Translate line by line. Never merge two lines, never split one, never drop \
one, never add one.

Answer with one line per input line, in the same order, formatted exactly:
<the line's number>|<the translation>
Nothing else - no commentary, no blank lines, no markdown."""


def existing(db: Session, meeting_id: uuid.UUID) -> list[str]:
    """Languages this meeting has already been translated into."""
    return [
        row.language
        for row in db.execute(
            select(TranscriptTranslation).where(TranscriptTranslation.meeting_id == meeting_id)
        ).scalars()
    ]


def get(db: Session, meeting_id: uuid.UUID, language: str) -> TranscriptTranslation | None:
    return db.execute(
        select(TranscriptTranslation).where(
            TranscriptTranslation.meeting_id == meeting_id,
            TranscriptTranslation.language == language,
        )
    ).scalar_one_or_none()


def clear(db: Session, meeting_id: uuid.UUID) -> None:
    """Drop every translation - the transcript they were made from has changed."""
    for row in db.execute(
        select(TranscriptTranslation).where(TranscriptTranslation.meeting_id == meeting_id)
    ).scalars():
        db.delete(row)


def translate(
    db: Session,
    meeting: Meeting,
    language: str,
    on_progress=None,
    should_cancel=None,
) -> TranscriptTranslation:
    """Translate the whole transcript into `language` and store it.

    `should_cancel` is checked between batches and before each one starts, so
    stopping takes at most one in-flight model call. Nothing is written when a
    translation is cancelled - a half-translated transcript is worse than none,
    because it would look finished.
    """
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language {language!r}. Supported: {', '.join(LANGUAGES)}")
    report = on_progress or (lambda _fraction, _message: None)
    cancelled = should_cancel or (lambda: False)

    rows = db.execute(
        select(Segment).where(Segment.meeting_id == meeting.id).order_by(Segment.idx)
    ).scalars().all()
    if not rows:
        raise ValueError("This meeting has no transcript to translate")

    provider, model, api_key = _provider(db)
    # No database locks held across the model calls.
    db.commit()

    batches = [rows[i : i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    translated: dict[int, str] = {}
    total = len(batches)
    name = LANGUAGES[language]
    report(0.01, f"Translating {len(rows)} lines into {name} — {total} parts at once")

    def run(batch):
        # Checked here too: with more batches than threads, the ones still
        # waiting for a thread should never start after a cancellation.
        if cancelled():
            raise TranslationCancelled()
        return _translate_batch(batch, language, provider, model, api_key)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, total)) as pool:
        futures = [pool.submit(run, batch) for batch in batches]
        try:
            for done, future in enumerate(as_completed(futures), start=1):
                # A failed batch raises here and fails the whole translation
                # rather than quietly storing a half-translated transcript.
                translated.update(future.result())
                if cancelled():
                    raise TranslationCancelled()
                report(done / total, f"Translating into {name} — {done} of {total} parts done")
        except Exception:
            # Drop whatever has not started; the pool still waits for the calls
            # already in flight, which is why stopping is not instant.
            for future in futures:
                future.cancel()
            raise

    # Any line the model skipped keeps its original text, so the transcript
    # stays complete and the reader can see what was not translated.
    segments = [{"idx": row.idx, "text": translated.get(row.idx, row.text)} for row in rows]
    missing = sum(1 for row in rows if row.idx not in translated)
    if missing:
        log.warning("Translation into %s left %d of %d lines untranslated", language, missing, len(rows))

    existing_row = get(db, meeting.id, language)
    if existing_row is not None:
        db.delete(existing_row)
        db.flush()
    record = TranscriptTranslation(
        meeting_id=meeting.id, language=language, segments=segments, model=model
    )
    db.add(record)
    db.commit()
    report(1.0, f"{LANGUAGES[language]} transcript ready")
    return record


def _provider(db: Session) -> tuple[str, str, str]:
    """The minutes provider writes translations too - same key, same model."""
    provider = (credentials.resolve(db, "minutes_provider") or "anthropic").lower()
    if provider == "openai":
        return provider, credentials.resolve(db, "openai_model") or settings.openai_model, credentials.resolve(db, "openai_api_key")
    return provider, credentials.resolve(db, "anthropic_model") or settings.anthropic_model, credentials.resolve(db, "anthropic_api_key")


def _translate_batch(batch, language: str, provider: str, model: str, api_key: str) -> dict[int, str]:
    system = SYSTEM_PROMPT.format(language=LANGUAGES[language])
    body = "\n".join(f"{row.idx}|{row.text}" for row in batch)

    if provider == "openai":
        text = _openai(system, body, model, api_key)
    else:
        text = _anthropic(system, body, model, api_key)

    out: dict[int, str] = {}
    wanted = {row.idx for row in batch}
    for raw in text.splitlines():
        line = _LINE.match(raw)
        if not line:
            continue
        idx = int(line.group(1))
        if idx in wanted:
            out[idx] = line.group(2)
    return out


def _openai(system: str, body: str, model: str, api_key: str) -> str:
    if not api_key:
        raise RuntimeError(
            "No OpenAI API key is configured, so the transcript cannot be translated. "
            "Add one under Settings > AI providers."
        )
    import openai

    client = openai.OpenAI(api_key=api_key)
    response = client.responses.create(model=model, instructions=system, input=body)
    return response.output_text or ""


def _anthropic(system: str, body: str, model: str, api_key: str) -> str:
    if not api_key:
        raise RuntimeError(
            "No Anthropic API key is configured, so the transcript cannot be translated. "
            "Add one under Settings > AI providers."
        )
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": body}],
    )
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
