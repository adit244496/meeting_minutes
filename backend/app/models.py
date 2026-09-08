"""SQLAlchemy models.

Schema notes
------------
`segments` is the canonical transcript: one row per contiguous stretch of speech
by one speaker. `speaker_label` is always populated (the diarizer's anonymous
SPEAKER_00 etc.); `participants.user_id` is populated only when a voiceprint
matched above threshold. That split is deliberate - it lets the UI show
"Unknown Speaker 2" without losing the diarizer's confidence that those turns
came from the same person, and it makes post-hoc relabelling a one-row update
rather than a rewrite of every segment.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# ECAPA-TDNN (speechbrain/spkrec-ecapa-voxceleb) emits 192-dim embeddings.
SPEAKER_EMBEDDING_DIM = 192
# Reserved for multilingual text embeddings over meeting history (not yet wired).
TEXT_EMBEDDING_DIM = 384


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    member = "member"


class MeetingStatus(str, enum.Enum):
    created = "created"
    uploaded = "uploaded"
    processing = "processing"
    transcribed = "transcribed"
    completed = "completed"
    failed = "failed"


class AudioSource(str, enum.Enum):
    upload = "upload"
    browser_mic = "browser_mic"
    zoom = "zoom"
    teams = "teams"
    meet = "meet"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), default=Role.member)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    voiceprints: Mapped[list[Voiceprint]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Voiceprint(Base):
    """One enrolled voice sample.

    Store several per user (3 x ~20s of clean speech is the sweet spot) and take
    the best match across them rather than averaging into a single centroid -
    averaging washes out legitimate variation between a person's phone voice and
    their conference-room voice.
    """

    __tablename__ = "voiceprints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    embedding = mapped_column(Vector(SPEAKER_EMBEDDING_DIM), nullable=False)
    sample_key: Mapped[str] = mapped_column(String(512))
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # 'enrollment' = admin uploaded it up front; 'correction' = harvested from a
    # user relabelling an unknown speaker in a finished meeting.
    origin: Mapped[str] = mapped_column(String(32), default="enrollment")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="voiceprints")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="meeting_status"), default=MeetingStatus.created, index=True
    )
    source: Mapped[AudioSource] = mapped_column(
        Enum(AudioSource, name="audio_source"), default=AudioSource.upload
    )
    audio_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Set when the recording is purged by the retention job. The meeting, its
    # transcript and its minutes all survive - only the audio goes.
    audio_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    language_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    asr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    segments: Mapped[list[Segment]] = relationship(
        back_populates="meeting",
        cascade="all, delete-orphan",
        order_by="Segment.idx",
    )
    participants: Mapped[list[Participant]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    minutes: Mapped[Minutes | None] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", uselist=False
    )


class Participant(Base):
    """Resolution of one diarized cluster to a person (or explicitly, to nobody)."""

    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("meeting_id", "speaker_label"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    speaker_label: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(255))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # True when a human corrected the automatic match. Never overwrite these.
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    speaking_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    meeting: Mapped[Meeting] = relationship(back_populates="participants")


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, index=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    speaker_label: Mapped[str] = mapped_column(String(64), index=True)
    # Per-segment, not per-meeting: real meetings code-switch mid-conversation.
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Unicode scripts actually present, e.g. 'latn', 'deva+latn'. A '+' here is
    # intra-sentential code-switching - the case no vendor publishes WER for.
    scripts: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="segments")


class AppSetting(Base):
    """Admin-controlled feature toggles. See app/features.py for the catalogue."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Minutes(Base):
    __tablename__ = "minutes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), unique=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    action_items: Mapped[list] = mapped_column(JSON, default=list)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    languages_detected: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(64))
    # Reserved for semantic search over history - see README "Searching history".
    embedding = mapped_column(Vector(TEXT_EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    meeting: Mapped[Meeting] = relationship(back_populates="minutes")
