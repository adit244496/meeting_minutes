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


class MinutesOut(ORMModel):
    summary: str
    topics: list
    decisions: list
    action_items: list
    open_questions: list
    languages_detected: list
    model: str
    created_at: datetime


class MeetingDetail(MeetingOut):
    participants: list[ParticipantOut] = []
    segments: list[SegmentOut] = []
    minutes: MinutesOut | None = None


class RelabelRequest(BaseModel):
    speaker_label: str
    user_id: uuid.UUID | None = None
    display_name: str | None = None
    # Harvest this speaker's audio as a new enrollment sample, so the next
    # meeting recognises them automatically.
    enroll: bool = True
