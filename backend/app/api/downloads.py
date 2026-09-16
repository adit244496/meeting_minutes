"""Downloads for each tier of meeting data.

Retention deletes audio, then transcripts, then (rarely) minutes. Anything that
will eventually be deleted has to be exportable first, so each tier gets a
download in a format people actually use elsewhere: plain text and subtitles for
the transcript, Markdown for the minutes, JSON for anything machine-readable.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access, storage, transcripts
from app.db import get_db
from app.deps import current_user
from app.models import AudioSource, Meeting, Minutes, Participant, Segment, User

router = APIRouter(prefix="/api/meetings", tags=["downloads"])
# Its own prefix: "/api/meetings/export" would be captured by the
# "/api/meetings/{meeting_id}" route and rejected as an invalid UUID.
export_router = APIRouter(prefix="/api/exports", tags=["downloads"])


@export_router.get("/meetings")
def export_meetings(
    kind: str = Query(pattern="^(transcript|minutes|both)$"),
    days: int = Query(default=7, ge=0, le=3650, description="Look-back window; 0 = all time"),
    language: str | None = Query(
        default=None,
        pattern="^(en|bn|hi)$",
        description="Use each meeting's translation into this language where one exists.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Every transcript and/or set of minutes from a timeframe, as one ZIP.

    One file per meeting, in the same format as the single-meeting download,
    so the archive is readable without any tooling. Only the meetings this
    person could open one at a time go into it.
    """
    query = select(Meeting).order_by(Meeting.started_at.desc())
    allowed = access.visible_clause(user)
    if allowed is not None:
        query = query.where(allowed)
    if days:
        query = query.where(Meeting.started_at >= datetime.now(timezone.utc) - timedelta(days=days))
    meetings = list(db.execute(query).scalars())

    buffer = io.BytesIO()
    written = 0
    used: set[str] = set()

    def unique(name: str) -> str:
        # Two meetings with the same title on the same day would otherwise
        # overwrite each other inside the archive.
        stem, _, ext = name.rpartition(".")
        candidate, n = name, 2
        while candidate in used:
            candidate, n = f"{stem}-{n}.{ext}", n + 1
        used.add(candidate)
        return candidate

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for meeting in meetings:
            if kind in ("transcript", "both"):
                rows = _segments(db, meeting.id)
                if rows:
                    # Falls back to the original per meeting: an archive that
                    # skipped everything not yet translated would silently be
                    # missing meetings.
                    version = _Version(None, None, {})
                    if language:
                        row = transcripts.get(db, meeting.id, language)
                        if row is not None:
                            version = _Version(
                                language,
                                row.model,
                                {int(s["idx"]): s["text"] for s in (row.segments or []) if "idx" in s},
                            )
                    text = _transcript_text(meeting, rows, _speaker_names(db, meeting.id), version)
                    folder = "transcripts/" if kind == "both" else ""
                    archive.writestr(
                        folder + unique(_filename(meeting, "txt", version.label)), text
                    )
                    written += 1
            if kind in ("minutes", "both"):
                folder = "minutes/" if kind == "both" else ""
                for minutes in db.execute(
                    select(Minutes).where(Minutes.meeting_id == meeting.id).order_by(Minutes.kind.desc())
                ).scalars():
                    # Word, since that is what minutes get forwarded as.
                    archive.writestr(
                        folder + unique(_filename(meeting, "docx", f"minutes-{minutes.kind}")),
                        _minutes_docx(meeting, minutes),
                    )
                    written += 1

    if not written:
        window = "in that timeframe" if days else "yet"
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {kind if kind != 'both' else 'transcripts or minutes'} {window}")

    span = f"last-{days}-days" if days else "all-time"
    label = {"transcript": "transcripts", "minutes": "minutes", "both": "transcripts-and-minutes"}[kind]
    tongue = f"-{language}" if language else ""
    filename = f"neo-minutes-{label}{tongue}-{span}-{datetime.now().strftime('%Y-%m-%d')}.zip"
    return _attachment(buffer.getvalue(), filename, "application/zip")


def _slug(text: str) -> str:
    """A filename stem that survives every OS, without mangling Indic titles."""
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned[:60].strip("-").lower() or "meeting"


def _filename(meeting: Meeting, suffix: str, label: str = "") -> str:
    stamp = _local(meeting.started_at or datetime.now()).strftime("%Y-%m-%d")
    tail = f"-{label}" if label else ""
    return f"{stamp}-{_slug(meeting.title)}{tail}.{suffix}"


def _attachment(content: str | bytes, filename: str, media_type: str) -> Response:
    body = content.encode("utf-8") if isinstance(content, str) else content
    return Response(
        content=body,
        media_type=media_type,
        headers={
            # RFC 5987 form as well, so a Hindi or Bengali title downloads with
            # its own name rather than a row of question marks.
            "Content-Disposition": (
                f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'download'}\"; "
                f"filename*=UTF-8''{filename}"
            )
        },
    )


def _load(db: Session, meeting_id: uuid.UUID, user: User) -> Meeting:
    return access.load_meeting(db, meeting_id, user)


def _segments(db: Session, meeting_id: uuid.UUID) -> list[Segment]:
    return list(
        db.execute(
            select(Segment).where(Segment.meeting_id == meeting_id).order_by(Segment.idx)
        ).scalars()
    )


def _speaker_names(db: Session, meeting_id: uuid.UUID) -> dict[str, str]:
    return {
        p.speaker_label: p.display_name
        for p in db.execute(
            select(Participant).where(Participant.meeting_id == meeting_id)
        ).scalars()
    }


def _local(when: datetime) -> datetime:
    """UTC in the database, the server's own clock in anything people read.

    Times are stored in UTC, and the browser converts them for the viewer. A
    downloaded file has no such chance: whatever is written into it is what the
    reader sees. For a single-site deployment the server's timezone is the one
    the meeting happened in, which is the only reading that will not confuse
    somebody comparing the transcript against their calendar.
    """
    return when.astimezone() if when.tzinfo else when


def _stamp(meeting: Meeting, ms: int) -> str:
    """How a line's time is written in a transcript people read.

    A meeting recorded here started when `started_at` says it did, so the clock
    time answers "when was this said". An uploaded file's `started_at` is when
    somebody uploaded it - possibly days after the meeting - so only the offset
    into the recording means anything.
    """
    if meeting.source == AudioSource.browser_mic and meeting.started_at:
        return _local(meeting.started_at + timedelta(milliseconds=ms)).strftime("%H:%M:%S")
    return _clock(ms)


def _clock(ms: int, srt: bool = False) -> str:
    total, millis = divmod(max(0, ms), 1000)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@router.get("/{meeting_id}/download/transcript")
def download_transcript(
    meeting_id: uuid.UUID,
    fmt: str = Query(default="txt", pattern="^(txt|json|srt|docx)$"),
    language: str | None = Query(
        default=None,
        pattern="^(en|bn|hi)$",
        description="Download a translation instead of the original. It must exist already.",
    ),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """The transcript, in the original or in a language it has been translated into.

    A translation only ever replaces the words. Timestamps, speakers and the
    per-line source language are the same record either way, so the downloaded
    file is as complete as the original - and says on its face that it is a
    translation, by which model, so nobody mistakes it for what was said.
    """
    meeting = _load(db, meeting_id, user)
    rows = _segments(db, meeting_id)
    if not rows:
        detail = (
            "The transcript was deleted under the retention policy"
            if meeting.transcript_deleted_at
            else "This meeting has no transcript yet"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    names = _speaker_names(db, meeting_id)
    version = _version(db, meeting_id, language)

    if fmt == "docx":
        return _attachment(
            _transcript_docx(meeting, rows, names, version),
            _filename(meeting, "docx", version.label),
            DOCX_TYPE,
        )

    if fmt == "txt":
        return _attachment(
            _transcript_text(meeting, rows, names, version),
            _filename(meeting, "txt", version.label),
            "text/plain; charset=utf-8",
        )

    if fmt == "json":
        body = json.dumps(
            {
                "title": meeting.title,
                "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
                "duration_seconds": meeting.duration_seconds,
                "asr_provider": meeting.asr_provider,
                "language": version.language,
                "translated_by": version.model,
                "segments": [
                    {
                        "start_ms": s.start_ms,
                        "end_ms": s.end_ms,
                        "speaker": names.get(s.speaker_label, s.speaker_label),
                        "speaker_label": s.speaker_label,
                        "language": s.language,
                        "scripts": s.scripts,
                        "text": version.text(s),
                        # Kept beside the translation rather than replaced: a
                        # machine-readable transcript that has quietly lost what
                        # was actually said is not much of a record.
                        **({"original_text": s.text} if version.translated else {}),
                    }
                    for s in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        return _attachment(
            body, _filename(meeting, "json", version.label), "application/json; charset=utf-8"
        )

    blocks = []
    for i, s in enumerate(rows, start=1):
        who = names.get(s.speaker_label, s.speaker_label)
        blocks.append(
            f"{i}\n{_clock(s.start_ms, srt=True)} --> {_clock(s.end_ms, srt=True)}\n"
            f"{who}: {version.text(s)}\n"
        )
    return _attachment(
        "\n".join(blocks),
        _filename(meeting, "srt", version.label),
        "application/x-subrip; charset=utf-8",
    )


@dataclass(frozen=True)
class _Version:
    """Which wording of the transcript a download is asking for."""

    language: str | None
    model: str | None
    # idx -> translated line. Empty for the original.
    lines: dict[int, str]

    @property
    def translated(self) -> bool:
        return self.language is not None

    @property
    def label(self) -> str:
        return f"transcript-{self.language}" if self.language else "transcript"

    def text(self, segment: Segment) -> str:
        # A line the model skipped keeps the original, exactly as the page
        # shows it - a gap would be worse than an untranslated sentence.
        return self.lines.get(segment.idx, segment.text) if self.lines else segment.text

    def note(self) -> str:
        if not self.translated:
            return ""
        name = transcripts.LANGUAGES.get(self.language, self.language)
        by = f" by {self.model}" if self.model else ""
        return f"{name} translation{by}. The original was left as spoken."


def _version(db: Session, meeting_id: uuid.UUID, language: str | None) -> _Version:
    if not language:
        return _Version(None, None, {})
    row = transcripts.get(db, meeting_id, language)
    if row is None:
        name = transcripts.LANGUAGES.get(language, language)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"This transcript has not been translated into {name} yet. "
            "Translate it on the meeting page first, then download it.",
        )
    return _Version(
        language,
        row.model,
        {int(s["idx"]): s["text"] for s in (row.segments or []) if "idx" in s},
    )


def _transcript_text(
    meeting: Meeting,
    rows: list[Segment],
    names: dict[str, str],
    version: _Version | None = None,
) -> str:
    version = version or _Version(None, None, {})
    lines = [meeting.title, "=" * len(meeting.title)]
    if meeting.started_at:
        lines.append(_local(meeting.started_at).strftime("%d %B %Y, %H:%M"))
    if version.translated:
        lines.append(version.note())
    lines.append("")
    for s in rows:
        who = names.get(s.speaker_label, s.speaker_label)
        lines.append(f"[{_stamp(meeting, s.start_ms)}] {who}: {version.text(s)}")
    return "\n".join(lines) + "\n"


def _minutes_markdown(meeting: Meeting, minutes: Minutes) -> str:
    out = [f"# {meeting.title}", ""]
    if meeting.started_at:
        out.append(_local(meeting.started_at).strftime("%d %B %Y, %H:%M"))
        out.append("")
    if meeting.agenda:
        out += ["## Agenda", "", meeting.agenda, ""]

    out += ["## Summary", "", minutes.summary, ""]

    if minutes.key_points:
        out += ["## Key highlights", ""]
        out += [f"- **{p}**" for p in minutes.key_points]
        out.append("")

    if minutes.follow_ups:
        out += ["## Follow-ups from the previous meeting", ""]
        for f in minutes.follow_ups:
            status_key = f.get("status") or "not_discussed"
            note = f" — {f['note']}" if f.get("note") else ""
            out.append(f"- **{_FOLLOW_UP_LABEL.get(status_key, status_key)}:** {f.get('item', '')}{note}")
        out.append("")

    if minutes.decisions:
        out.append("## Decisions")
        out.append("")
        for d in minutes.decisions:
            who = f" — {d['decided_by']}" if d.get("decided_by") else ""
            out.append(f"- **{d.get('decision', '')}**{who}")
            if d.get("rationale"):
                out.append(f"  - {d['rationale']}")
        out.append("")

    if minutes.action_items:
        out.append("## Action items")
        out.append("")
        for a in minutes.action_items:
            due = f" (due {a['due']})" if a.get("due") else ""
            out.append(f"- [ ] {a.get('task', '')} — {a.get('owner', 'Unassigned')}{due}")
        out.append("")

    if minutes.topics:
        out.append("## Topics")
        out.append("")
        for t in minutes.topics:
            out.append(f"### {t.get('title', '')}")
            out.append("")
            out.append(t.get("discussion", ""))
            out.append("")

    if minutes.open_questions:
        out.append("## Open questions")
        out.append("")
        out += [f"- {q}" for q in minutes.open_questions]
        out.append("")

    out.append(f"*Version {minutes.version} ({minutes.source}), generated by {minutes.model}.*")
    return "\n".join(out) + "\n"


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(build) -> bytes:
    """Build a Word document with `build(doc)` and return its bytes."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    build(doc)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# Section colours, matching the minutes view on the meeting page.
_DOCX_COLORS = {
    "accent": "2C5F8A",
    "ok": "17795A",
    "warn": "8C5C10",
    "plum": "7B4FB3",
    "err": "B03028",
    "muted": "7B8798",
}
_PRIORITY_COLOR = {"high": "err", "medium": "warn", "low": "muted"}
_FOLLOW_UP_LABEL = {
    "done": "Done",
    "in_progress": "In progress",
    "not_started": "Not started",
    "blocked": "Blocked",
    "dropped": "Dropped",
    "not_discussed": "Not discussed",
}
_FOLLOW_UP_COLOR = {
    "done": "ok",
    "in_progress": "accent",
    "not_started": "warn",
    "blocked": "err",
    "dropped": "muted",
    "not_discussed": "muted",
}


def _shade(element, fill: str) -> None:
    """Background fill for a paragraph or table cell (python-docx has no API for it)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    props = element.get_or_add_pPr() if hasattr(element, "get_or_add_pPr") else element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    props.append(shd)


def _run(paragraph, text: str, *, bold=False, color: str | None = None, size: float | None = None, italic=False):
    from docx.shared import Pt, RGBColor

    run = paragraph.add_run(text)
    run.bold = bold
    run.italic = italic
    if color:
        run.font.color.rgb = RGBColor.from_string(_DOCX_COLORS.get(color, color))
    if size:
        run.font.size = Pt(size)
    return run


def _section(doc, title: str, tone: str, count: int | None = None) -> None:
    from docx.shared import Pt

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(16)
    p.paragraph_format.space_after = Pt(6)
    _run(p, title.upper(), bold=True, color=tone, size=12)
    if count is not None:
        _run(p, f"   {count}", bold=True, color="muted", size=11)


def _minutes_docx(meeting: Meeting, minutes: Minutes) -> bytes:
    def build(doc) -> None:
        from docx.shared import Pt

        doc.add_heading(meeting.title, level=0)
        meta = [f"{minutes.kind.capitalize()} minutes"]
        if meeting.started_at:
            meta.append(_local(meeting.started_at).strftime("%d %B %Y, %H:%M"))
        meta.append(f"v{minutes.version}")
        _run(doc.add_paragraph(), " · ".join(meta), color="muted", size=10)

        glance = doc.add_paragraph()
        for label, n, tone in (
            ("Decisions", len(minutes.decisions or []), "ok"),
            ("Action items", len(minutes.action_items or []), "accent"),
            ("Open questions", len(minutes.open_questions or []), "warn"),
        ):
            _run(glance, f"{n} ", bold=True, color=tone, size=12)
            _run(glance, f"{label}     ", color=tone, size=10)

        if meeting.agenda:
            _section(doc, "Agenda", "muted")
            for line in meeting.agenda.splitlines():
                if line.strip():
                    p = doc.add_paragraph()
                    p.paragraph_format.space_after = Pt(1)
                    _run(p, line.strip(), size=10.5, color="muted")

        _section(doc, "Summary", "accent")
        summary = doc.add_paragraph()
        summary.paragraph_format.left_indent = Pt(6)
        summary.paragraph_format.space_after = Pt(4)
        _shade(summary._p, "E7F0F8")
        _run(summary, minutes.summary, size=11.5)

        if minutes.key_points:
            _section(doc, "Key highlights", "warn")
            for point in minutes.key_points:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(2)
                _shade(p._p, "FBEED6")
                _run(p, "★  ", bold=True, color="warn")
                _run(p, point, bold=True)

        if minutes.follow_ups:
            _section(doc, "Follow-ups from the previous meeting", "plum", len(minutes.follow_ups))
            table = doc.add_table(rows=1, cols=3)
            table.style = "Table Grid"
            for cell, label in zip(table.rows[0].cells, ("Previous item", "Status", "Note")):
                cell.text = ""
                _run(cell.paragraphs[0], label, bold=True, color="FFFFFF", size=10)
                _shade(cell._tc, _DOCX_COLORS["plum"])
            for f in minutes.follow_ups:
                row = table.add_row().cells
                owner = f" ({f['owner']})" if f.get("owner") else ""
                _run(row[0].paragraphs[0], f.get("item", "") + owner, size=10.5)
                status_key = f.get("status") or "not_discussed"
                _run(row[1].paragraphs[0], _FOLLOW_UP_LABEL.get(status_key, status_key), bold=True,
                     color=_FOLLOW_UP_COLOR.get(status_key, "muted"), size=10)
                _run(row[2].paragraphs[0], f.get("note") or "—", size=10)

        if minutes.decisions:
            _section(doc, "Decisions", "ok", len(minutes.decisions))
            for d in minutes.decisions:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(2)
                _run(p, "✔  ", bold=True, color="ok")
                _run(p, d.get("decision", ""), bold=True)
                if d.get("decided_by"):
                    _run(p, f"  — {d['decided_by']}", color="muted", size=10)
                if d.get("rationale"):
                    why = doc.add_paragraph()
                    why.paragraph_format.left_indent = Pt(20)
                    why.paragraph_format.space_after = Pt(6)
                    _run(why, "Why: ", bold=True, color="muted", size=10)
                    _run(why, d["rationale"], size=10.5)

        if minutes.action_items:
            _section(doc, "Action items", "accent", len(minutes.action_items))
            table = doc.add_table(rows=1, cols=4)
            table.style = "Table Grid"
            for cell, label in zip(table.rows[0].cells, ("Task", "Owner", "Due", "Priority")):
                cell.text = ""
                _run(cell.paragraphs[0], label, bold=True, color="FFFFFF", size=10)
                _shade(cell._tc, _DOCX_COLORS["accent"])
            for a in minutes.action_items:
                row = table.add_row().cells
                _run(row[0].paragraphs[0], "☐  " + a.get("task", ""), size=10.5)
                _run(row[1].paragraphs[0], a.get("owner", "Unassigned"), bold=True, size=10)
                _run(row[2].paragraphs[0], a.get("due") or "—", size=10)
                priority = a.get("priority") or "medium"
                _run(row[3].paragraphs[0], priority.capitalize(), bold=True,
                     color=_PRIORITY_COLOR.get(priority, "muted"), size=10)

        if minutes.topics:
            _section(doc, "Topics discussed", "plum", len(minutes.topics))
            for i, t in enumerate(minutes.topics, 1):
                head = doc.add_paragraph()
                head.paragraph_format.space_after = Pt(1)
                _run(head, f"{i}. ", bold=True, color="plum")
                _run(head, t.get("title", ""), bold=True)
                body = doc.add_paragraph()
                body.paragraph_format.left_indent = Pt(16)
                _run(body, t.get("discussion", ""), size=10.5)
                if t.get("speakers"):
                    _run(body, "\nSpeakers: " + ", ".join(t["speakers"]), italic=True, color="muted", size=9.5)

        if minutes.open_questions:
            _section(doc, "Open questions", "warn", len(minutes.open_questions))
            for q in minutes.open_questions:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(3)
                _run(p, "?  ", bold=True, color="warn")
                _run(p, q)

    return _docx_bytes(build)


def _transcript_docx(
    meeting: Meeting,
    rows: list[Segment],
    names: dict[str, str],
    version: _Version | None = None,
) -> bytes:
    version = version or _Version(None, None, {})

    def build(doc) -> None:
        doc.add_heading(meeting.title, level=0)
        meta = []
        if meeting.started_at:
            meta.append(_local(meeting.started_at).strftime("%d %B %Y, %H:%M"))
        meta.append(
            f"Transcript — {transcripts.LANGUAGES.get(version.language, version.language)}"
            if version.translated
            else "Transcript"
        )
        doc.add_paragraph(" · ".join(meta))
        if version.translated:
            _run(doc.add_paragraph(), version.note(), italic=True, color="muted", size=9.5)
        for s in rows:
            p = doc.add_paragraph()
            p.add_run(f"[{_stamp(meeting, s.start_ms)}] ").italic = True
            p.add_run(f"{names.get(s.speaker_label, s.speaker_label)}: ").bold = True
            p.add_run(version.text(s))

    return _docx_bytes(build)


@router.get("/{meeting_id}/download/minutes")
def download_minutes(
    meeting_id: uuid.UUID,
    fmt: str = Query(default="docx", pattern="^(md|json|docx)$"),
    kind: str = Query(default="short", pattern="^(short|detailed)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    meeting = _load(db, meeting_id, user)
    minutes = db.execute(
        select(Minutes).where(Minutes.meeting_id == meeting_id, Minutes.kind == kind)
    ).scalar_one_or_none()
    if minutes is None:
        detail = (
            "The minutes were deleted under the retention policy"
            if meeting.minutes_deleted_at
            else f"{kind.capitalize()} minutes have not been generated yet"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    if fmt == "docx":
        return _attachment(_minutes_docx(meeting, minutes), _filename(meeting, "docx", f"minutes-{kind}"), DOCX_TYPE)

    if fmt == "json":
        body = json.dumps(
            {
                "title": meeting.title,
                "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
                "kind": minutes.kind,
                "version": minutes.version,
                "source": minutes.source,
                "model": minutes.model,
                "summary": minutes.summary,
                "topics": minutes.topics,
                "decisions": minutes.decisions,
                "action_items": minutes.action_items,
                "open_questions": minutes.open_questions,
            },
            ensure_ascii=False,
            indent=2,
        )
        return _attachment(body, _filename(meeting, "json", f"minutes-{kind}"), "application/json; charset=utf-8")

    return _attachment(
        _minutes_markdown(meeting, minutes),
        _filename(meeting, "md", f"minutes-{kind}"),
        "text/markdown; charset=utf-8",
    )


@router.get("/{meeting_id}/download/audio")
def download_audio(
    meeting_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    meeting = _load(db, meeting_id, user)
    if not meeting.audio_key:
        detail = (
            "The recording was deleted under the retention policy"
            if meeting.audio_deleted_at
            else "This meeting has no audio"
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    try:
        stream = storage.open_stream(meeting.audio_key)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file is missing") from None

    suffix = meeting.audio_key.rsplit(".", 1)[-1].lower() or "bin"
    media_type = {
        "webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
        "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")

    filename = _filename(meeting, suffix)
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename.encode('ascii', 'ignore').decode() or 'recording'}\"; "
                f"filename*=UTF-8''{filename}"
            )
        },
    )
