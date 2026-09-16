"""Meeting minutes generation with Claude or OpenAI (admin's choice).

A 2-hour meeting transcript is roughly 30K tokens, so it fits in one call
against the 1M-token context window - no chunking, no map-reduce, no loss of
cross-meeting context. Structured outputs give back a validated object instead
of prose we would have to re-parse.

Cost, at Opus 5 rates ($5/MTok in, $25/MTok out): ~30K in + ~3K out works out to
roughly $0.20-0.25 per meeting.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Literal

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


class FollowUp(BaseModel):
    item: str = Field(description="The action item or open question from the previous meeting.")
    kind: Literal["action_item", "open_question"] = "action_item"
    owner: str | None = Field(default=None, description="Owner from the previous meeting, if any.")
    status: Literal["done", "in_progress", "not_started", "blocked", "dropped", "not_discussed"] = Field(
        description="What this meeting's transcript says about it. Use not_discussed "
        "when it was not mentioned - never guess progress."
    )
    note: str | None = Field(default=None, description="One line of evidence from this meeting.")


class MeetingMinutes(BaseModel):
    summary: str = Field(description="A tight 3-5 sentence executive summary.")
    key_points: list[str] = Field(
        default_factory=list,
        description="The 3-5 most important takeaways, one short line each - what a "
        "reader must not miss.",
    )
    topics: list[Topic] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    follow_ups: list[FollowUp] = Field(
        default_factory=list,
        description="Only when a previous meeting is provided: one entry per previous "
        "action item and open question. Empty otherwise.",
    )
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


PROVIDERS = ("anthropic", "openai")

# Called with (fraction done 0..1, human message).
ProgressFn = Callable[[float, str], None]

# The structured output arrives in schema order, so the section currently being
# written is a real measure of how far through the minutes the model is.
# (JSON key, label, fraction when that section starts)
_SECTIONS = (
    ("summary", "Writing the summary", 0.35),
    ("key_points", "Picking the key highlights", 0.42),
    ("topics", "Writing topics discussed", 0.50),
    ("decisions", "Recording decisions", 0.64),
    ("action_items", "Listing action items", 0.74),
    ("follow_ups", "Checking last meeting's follow-ups", 0.84),
    ("open_questions", "Noting open questions", 0.92),
    ("languages_detected", "Finishing up", 0.97),
)
# Counted inside a section to say "3 so far", one marker key per item.
_ITEM_MARKER = {
    "topics": '"discussion"',
    "decisions": '"decision"',
    "action_items": '"task"',
    "follow_ups": '"status"',
}


class _Tracker:
    """Turns a stream of output text into progress messages.

    Before any output there is nothing to measure - the model is reading the
    transcript and, with thinking on, reasoning about it - so a heartbeat
    reports elapsed time instead of a frozen bar.
    """

    def __init__(self, report: ProgressFn, provider_name: str) -> None:
        self.report = report
        self.provider_name = provider_name
        self.text: list[str] = []
        self.thinking = False
        self.started = time.monotonic()
        self.last = 0.0
        self.section = -1
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)

    def __enter__(self) -> "_Tracker":
        self.report(0.05, f"Sending the transcript to {self.provider_name}")
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self.started)
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _heartbeat(self) -> None:
        while not self._stop.wait(3.0):
            if self.text:
                continue
            # Creep towards 30% so a long silent read or reasoning phase still
            # visibly moves. OpenAI reasoning models emit no events at all here.
            crept = min(0.30, 0.08 + (time.monotonic() - self.started) / 200)
            if self.thinking:
                self.report(max(crept, 0.15), f"Analysing the discussion… {self._elapsed()}")
            else:
                self.report(crept, f"{self.provider_name} is reading and analysing the transcript… {self._elapsed()}")

    def on_thinking(self) -> None:
        if not self.thinking:
            self.thinking = True
            self.report(0.15, f"Analysing the discussion… {self._elapsed()}")

    def on_text(self, delta: str) -> None:
        if not delta:
            return
        self.text.append(delta)
        now = time.monotonic()
        if now - self.last < 0.8:
            return
        self.last = now

        so_far = "".join(self.text)
        current = -1
        for index, (key, _, _) in enumerate(_SECTIONS):
            if f'"{key}"' in so_far:
                current = index
        if current < 0:
            self.report(0.32, "Starting to write the minutes")
            return

        key, label, start = _SECTIONS[current]
        end = _SECTIONS[current + 1][2] if current + 1 < len(_SECTIONS) else 0.99
        body = so_far[so_far.rfind(f'"{key}"'):]
        marker = _ITEM_MARKER.get(key)
        count = body.count(marker) if marker else 0
        # Move within the section as it grows, without reaching the next one.
        within = min(len(body) / 1500, 0.9)
        message = f"{label} ({count} so far)" if count else label
        self.report(start + (end - start) * within, message)


def generate_minutes(
    title: str,
    segments: list[dict],
    output_language: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    on_progress: ProgressFn | None = None,
    kind: str = "detailed",
    previous: dict | None = None,
    agenda: str | None = None,
) -> MeetingMinutes:
    """Produce structured minutes for one meeting.

    `agenda` is what the organiser said the meeting was for. It steers the
    summary towards the points that were meant to be covered - and makes the
    ones that were not visible as open questions.

    `previous` is the prior meeting of the same series - {title, date,
    summary, decisions, action_items, open_questions} - so these minutes can
    report what became of its action items and questions.

    `segments` is a list of {start_ms, speaker, text, language} dicts - the
    resolved transcript, with speaker labels already replaced by real names
    where identification succeeded.

    `provider`, `api_key` and `model` come from the admin panel when the caller
    has a database session; all fall back to the environment.
    """
    provider = (provider or settings.minutes_provider or "anthropic").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown minutes provider {provider!r}. Supported: {', '.join(PROVIDERS)}")

    lang_code = output_language or settings.minutes_language
    lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)

    if not segments:
        raise ValueError("cannot generate minutes from an empty transcript")

    short = kind == "short"
    prompt = (
        f"Meeting title: {title}\n\n"
        + _agenda_block(agenda)
        + f"Write the minutes entirely in {lang_name}, whatever languages were spoken. "
        f"Translate any direct quotes into {lang_name} too; do not copy text in "
        f"another language or script.\n\n"
        + (SHORT_INSTRUCTIONS if short else DETAILED_INSTRUCTIONS)
        + _previous_block(previous)
        + f"\n\nTranscript:\n{_format_transcript(segments)}"
    )

    report = on_progress or (lambda _fraction, _message: None)

    def attempt():
        if provider == "openai":
            return _generate_openai(
                title,
                prompt,
                model=model or settings.openai_model,
                api_key=api_key or settings.openai_api_key,
                report=report,
            )
        return _generate_anthropic(
            title,
            prompt,
            model=model or settings.anthropic_model,
            api_key=api_key or settings.anthropic_api_key,
            report=report,
            # A brief is a compression task, not an analysis one; skipping
            # extended thinking makes the automatic short minutes faster.
            think=not short,
            max_tokens=4000 if short else 16000,
        )

    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 - re-raised unless transient
            if number == MAX_ATTEMPTS or not _is_transient(exc):
                raise
            log.warning("Minutes attempt %d failed transiently (%s); retrying", number, exc)
            for left in range(RETRY_WAIT_SECONDS, 0, -1):
                if left == RETRY_WAIT_SECONDS or left % 5 == 0:
                    report(0.05, f"Connection to the AI provider dropped. Retrying in {left}s")
                time.sleep(1)
    raise AssertionError("unreachable")


MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 10


def _is_transient(exc: Exception) -> bool:
    """A dropped stream, a timeout, or an overloaded/rate-limited provider.

    Streams are long-lived, and a provider or network closing one midway
    ("peer closed connection") is worth a retry; a bad key or bad request is not.
    """
    try:
        import openai

        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return True
        if isinstance(exc, openai.APIStatusError) and exc.status_code in (429, 500, 502, 503, 504):
            return True
    except ImportError:
        pass
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code in (429, 500, 502, 503, 504, 529):
        return True
    return False


def _agenda_block(agenda: str | None) -> str:
    """What the organiser said the meeting was for.

    Deliberately framed as intent rather than fact: an agenda says what was
    meant to happen, and a meeting often wanders off it. Treating it as a
    checklist would have the model report items that were never discussed.
    """
    if not agenda or not agenda.strip():
        return ""
    return (
        "The organiser set this agenda before the meeting. Use it to judge what "
        "matters, and follow the order of its points where the discussion allows. "
        "It is intent, not a record: write only what the transcript supports, and "
        "if an agenda point was not discussed, say so once in open_questions "
        "rather than inventing an outcome for it.\n"
        f"AGENDA:\n{agenda.strip()}\n\n"
    )


def _previous_block(previous: dict | None) -> str:
    """The previous meeting in the series, for the follow-up section."""
    if not previous:
        return "\n\nThis meeting has no previous meeting in a series: follow_ups must be an empty list."

    lines = [
        "",
        "",
        f"PREVIOUS MEETING IN THIS SERIES: {previous.get('title', '')} ({previous.get('date', '')})",
        f"Summary: {previous.get('summary', '')}",
    ]
    actions = previous.get("action_items") or []
    questions = previous.get("open_questions") or []
    if actions:
        lines.append("Action items:")
        lines += [f"- {a.get('task', '')} (owner: {a.get('owner') or 'Unassigned'})" for a in actions]
    if questions:
        lines.append("Open questions:")
        lines += [f"- {q}" for q in questions]
    lines.append(
        "follow_ups: one entry for EVERY action item and open question listed above, "
        "with its status according to THIS meeting's transcript. Use not_discussed when "
        "the transcript does not mention it; never infer progress. Put a one-line note "
        "with the evidence. Anything newly agreed still belongs in action_items too."
    )
    return "\n".join(lines)


SHORT_INSTRUCTIONS = """This is the SHORT version - a one-screen brief someone reads in 30 seconds.
- summary: 2-3 sentences, the outcome of the meeting, not a narrative of it.
- key_points: the 3 most important takeaways, a few words each.
- topics: return an empty list.
- decisions: only settled decisions, one short line each, no rationale.
- action_items: one short line per task with owner and due date if stated.
- open_questions: at most 3, only the important ones."""

DETAILED_INSTRUCTIONS = """This is the DETAILED version - a complete record of the meeting.
- summary: a tight 3-5 sentence executive summary.
- key_points: the 3-5 most important takeaways, one short line each.
- topics: every substantive topic, 2-4 sentences each, with who spoke to it.
- decisions: every settled decision, with rationale and who drove it where stated.
- action_items: every task, with owner, due date and priority where stated.
- open_questions: every question raised but left unresolved."""


def _missing_key(provider_name: str, env_var: str) -> RuntimeError:
    # Say this plainly rather than letting the SDK raise an auth error from
    # deep inside a worker task. Transcription uses a different provider, so a
    # meeting can transcribe perfectly and only fail at this step.
    return RuntimeError(
        f"No {provider_name} API key is configured, so minutes cannot be generated. "
        f"Add one under Settings > AI providers, or set {env_var} in .env - "
        "or set AUTO_GENERATE_MINUTES=false to keep transcripts only."
    )


def _strip_defaults(node):
    """OpenAI strict schemas reject `default`; the Pydantic model still applies them."""
    if isinstance(node, dict):
        return {k: _strip_defaults(v) for k, v in node.items() if k != "default"}
    if isinstance(node, list):
        return [_strip_defaults(v) for v in node]
    return node


def _generate_openai(
    title: str,
    prompt: str,
    model: str,
    api_key: str,
    report: ProgressFn,
) -> MeetingMinutes:
    if not api_key:
        raise _missing_key("OpenAI", "OPENAI_API_KEY")

    # Imported lazily so an install without the openai package still runs the
    # Anthropic path.
    import openai
    from openai.lib._pydantic import to_strict_json_schema

    client = openai.OpenAI(api_key=api_key)
    request = dict(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "meeting_minutes",
                "schema": _strip_defaults(to_strict_json_schema(MeetingMinutes)),
                "strict": True,
            }
        },
        stream=True,
    )
    response = None
    with _Tracker(report, "OpenAI") as tracker:
        # Streamed purely for progress; the result is identical to a plain call.
        stream = client.responses.create(**request)
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                tracker.on_text(event.delta)
            elif kind.startswith("response.reasoning"):
                tracker.on_thinking()
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                response = event.response

    if response is None:
        raise RuntimeError("OpenAI closed the stream without a final response")
    if response.status != "completed" or not response.output_text:
        detail = getattr(response, "incomplete_details", None) or getattr(response, "error", None)
        raise RuntimeError(f"OpenAI returned no minutes (status={response.status}, {detail})")
    minutes = MeetingMinutes.model_validate_json(response.output_text)
    report(1.0, "Minutes written")

    usage = response.usage
    log.info(
        "Generated minutes for %r with %s: %d topics, %d decisions, %d actions (in=%s out=%s tokens)",
        title,
        model,
        len(minutes.topics),
        len(minutes.decisions),
        len(minutes.action_items),
        usage.input_tokens if usage else "?",
        usage.output_tokens if usage else "?",
    )
    return minutes


def _generate_anthropic(
    title: str,
    prompt: str,
    model: str,
    api_key: str,
    report: ProgressFn,
    think: bool = True,
    max_tokens: int = 16000,
) -> MeetingMinutes:
    if not api_key:
        raise _missing_key("Anthropic", "ANTHROPIC_API_KEY")

    client = anthropic.Anthropic(api_key=api_key)
    # Adaptive thinking is an Opus/Sonnet 5 feature; smaller models like Haiku
    # reject the parameter, so they write the minutes without it.
    supports_adaptive = model.startswith(("claude-opus-5", "claude-sonnet-5"))
    extra = {"thinking": {"type": "adaptive"}} if think and supports_adaptive else {}

    with _Tracker(report, "Claude") as tracker:
        # Streamed purely for progress; the parsed result is identical to
        # messages.parse.
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=MeetingMinutes,
            **extra,
        ) as stream:
            for event in stream:
                if getattr(event, "type", "") != "content_block_delta":
                    continue
                delta = event.delta
                if delta.type == "thinking_delta":
                    tracker.on_thinking()
                elif delta.type == "text_delta":
                    tracker.on_text(delta.text)
            response = stream.get_final_message()

    minutes = response.parsed_output
    report(1.0, "Minutes written")
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
