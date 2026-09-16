from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import AudioSource, MeetingStatus, Role


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- users ----------


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=8)
    role: Role = Role.member


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserWithEnrollment(UserOut):
    voiceprint_count: int = 0
    enrolled_seconds: float = 0.0


class VoiceprintOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    duration_seconds: float
    origin: str
    created_at: datetime


# ---------- meetings ----------


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    source: AudioSource = AudioSource.upload
    # Leave null for mixed-language meetings; forcing one language is what
    # produces garbage transliteration on code-switched speech.
    language_hint: str | None = Field(default=None, max_length=16)
    asr_provider: str | None = None
    # Part of a recurring meeting: an existing series, or a new one by name.
    series_id: uuid.UUID | None = None
    new_series_name: str | None = Field(default=None, max_length=255)


class SeriesAssign(BaseModel):
    """Move a meeting into a series, a new series, or out of any series."""

    series_id: uuid.UUID | None = None
    new_series_name: str | None = Field(default=None, max_length=255)
    # When creating a series from a suggestion, the earlier look-alike meetings
    # to pull in with it.
    include_meeting_ids: list[uuid.UUID] = []


class SeriesOut(BaseModel):
    id: uuid.UUID
    name: str
    meeting_count: int
    last_meeting_at: datetime | None


class ParticipantOut(ORMModel):
    speaker_label: str
    user_id: uuid.UUID | None
    display_name: str
    confidence: float
    is_manual: bool
    speaking_seconds: float


class SegmentOut(ORMModel):
    idx: int
    start_ms: int
    end_ms: int
    speaker_label: str
    language: str | None
    scripts: str | None
    text: str


class MeetingOut(ORMModel):
    id: uuid.UUID
    title: str
    status: MeetingStatus
    source: AudioSource
    duration_seconds: float | None
    language_hint: str | None
    asr_provider: str | None
    error: str | None
    started_at: datetime
    processed_at: datetime | None
    audio_deleted_at: datetime | None
    transcript_deleted_at: datetime | None
    minutes_deleted_at: datetime | None
    series_id: uuid.UUID | None = None
    series_name: str | None = None
    is_live: bool = False
    live_transcribed_until: float | None = None
    has_recording: bool = False


class MinutesOut(ORMModel):
    kind: str = "detailed"
    summary: str
    key_points: list = []
    follow_ups: list = []
    topics: list
    decisions: list
    action_items: list
    open_questions: list
    languages_detected: list
    model: str
    created_at: datetime
    version: int = 1
    source: str = "generated"
    edited_at: datetime | None = None


class MinutesUpdate(BaseModel):
    """A hand-edited replacement for the current minutes.

    Everything is optional: the editor can save just the summary without having
    to echo back topics and action items it did not touch.
    """

    summary: str | None = None
    key_points: list | None = None
    follow_ups: list | None = None
    topics: list | None = None
    decisions: list | None = None
    action_items: list | None = None
    open_questions: list | None = None


class MinutesVersionOut(ORMModel):
    kind: str = "detailed"
    version: int
    source: str
    model: str
    created_at: datetime
    # Who made this version; null for machine-generated ones.
    created_by_name: str | None = None
    summary: str
    key_points: list = []
    follow_ups: list = []
    topics: list
    decisions: list
    action_items: list
    open_questions: list
    languages_detected: list


class MeetingDetail(MeetingOut):
    participants: list[ParticipantOut] = []
    segments: list[SegmentOut] = []
    # Zero, one or two entries: short and/or detailed.
    minutes: list[MinutesOut] = []


class RelabelRequest(BaseModel):
    speaker_label: str
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    # Harvest this speaker's audio as a new enrollment sample, so the next
    # meeting recognises them automatically.
    enroll: bool = True
