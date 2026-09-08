"""Meeting minutes generation with Claude.

A 2-hour meeting transcript is roughly 30K tokens, so it fits in one call
against the 1M-token context window - no chunking, no map-reduce, no loss of
cross-meeting context. Structured outputs give back a validated object instead
of prose we would have to re-parse.

Cost, at Opus 5 rates ($5/MTok in, $25/MTok out): ~30K in + ~3K out works out to
roughly $0.20-0.25 per meeting.
"""

from __future__ import annotations

import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from app.config import settings

log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi", "bn": "Bengali"}


class ActionItem(BaseModel):
    task: str = Field(description="What needs to be done, stated as an imperative.")
    owner: str = Field(
        description="Name of the person responsible, exactly as it appears in the "
        "transcript speaker labels. Use 'Unassigned' if nobody took ownership."
    )
    due: str | None = Field(
        default=None,
        description="Deadline if one was stated, otherwise null. Never invent one.",
    )
    priority: Literal["high", "medium", "low"] = "medium"


class Decision(BaseModel):
    decision: str = Field(description="The decision that was actually settled.")
    rationale: str | None = Field(default=None, description="Why, if it was stated.")
    decided_by: str | None = Field(default=None, description="Who drove the decision.")


class Topic(BaseModel):
    title: str
    discussion: str = Field(description="2-4 sentences on what was said.")
    speakers: list[str] = Field(default_factory=list)


class MeetingMinutes(BaseModel):
    summary: str = Field(description="A tight 3-5 sentence executive summary.")
    topics: list[Topic] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[str] = Field(
        default_factory=list,
        description="Questions raised but left unresolved.",
    )
    languages_detected: list[str] = Field(default_factory=list)


SYSTEM_PROMPT = """You write meeting minutes from raw, imperfect transcripts.

The transcripts come from automatic speech recognition of multilingual meetings \
(English, Hindi and Bengali, frequently code-switching mid-sentence). Treat them \
accordingly:

- ASR output contains errors. Where a word is clearly garbled but the meaning is \
recoverable from context, use the meaning. Where it is not recoverable, leave it out \
rather than guessing.
- Speaker labels come from automatic diarization. Some speakers are named; others \
appear as "Unknown Speaker N". Use the labels exactly as given - never invent a name \
for an unknown speaker, and never merge two labels on a hunch that they are the same \
person.
- Report only what was actually said. Do not infer decisions that were merely \
discussed, and do not assign an owner or a deadline that nobody stated. An empty \
action-items list is a correct answer for a meeting that produced no actions.
- Distinguish a decision (settled) from a topic (discussed) from an open question \
(raised, unresolved). This distinction is the main value of the minutes.
- Preserve technical terms, product names and numbers verbatim, including when the \
surrounding sentence is in Hindi or Bengali."""


def _format_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts = int(seg.get("start_ms", 0) // 1000)
        stamp = f"{ts // 3600:02d}:{(ts % 3600) // 60:02d}:{ts % 60:02d}"
        speaker = seg.get("speaker") or "Unknown"
        lang = seg.get("language")
        tag = f" [{lang}]" if lang else ""
        lines.append(f"[{stamp}] {speaker}{tag}: {seg.get('text', '')}")
    return "\n".join(lines)


def generate_minutes(
    title: str,
    segments: list[dict],
    output_language: str | None = None,
    model: str | None = None,
) -> MeetingMinutes:
    """Produce structured minutes for one meeting.

    `segments` is a list of {start_ms, speaker, text, language} dicts - the
    resolved transcript, with speaker labels already replaced by real names
    where identification succeeded.
    """
    model = model or settings.anthropic_model
    lang_code = output_language or settings.minutes_language
    lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)

    if not segments:
        raise ValueError("cannot generate minutes from an empty transcript")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    prompt = (
        f"Meeting title: {title}\n\n"
        f"Write the minutes in {lang_name}. Keep direct quotes in their original "
        f"language, followed by a short {lang_name} gloss in parentheses.\n\n"
        f"Transcript:\n{_format_transcript(segments)}"
    )

    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
        output_format=MeetingMinutes,
    )

    minutes = response.parsed_output
    if minutes is None:
        raise RuntimeError(f"Claude returned no parsed minutes (stop_reason={response.stop_reason})")

    log.info(
        "Generated minutes for %r: %d topics, %d decisions, %d actions (in=%s out=%s tokens)",
        title,
        len(minutes.topics),
        len(minutes.decisions),
        len(minutes.action_items),
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return minutes
