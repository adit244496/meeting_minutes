"""Admin user management and voice enrollment.

Enrollment is the admin-facing half of speaker identification: upload a few
clean samples per person before the meeting so the pipeline has something to
match diarized clusters against.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import features, storage
from app.audio import duration_seconds, to_wav16k_mono
from app.db import get_db
from app.deps import current_user, require_admin
from app.models import User, Voiceprint
from app.schemas import UserCreate, UserOut, UserWithEnrollment, VoiceprintOut
from app.security import hash_password
from app.speakers.embeddings import embed_file

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/users", tags=["users"])

# Below this, an embedding is too unstable to be worth enrolling.
MIN_ENROLLMENT_SECONDS = 5.0
# Recommended per-user total across all samples.
RECOMMENDED_ENROLLMENT_SECONDS = 60.0


@router.get("", response_model=list[UserWithEnrollment])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[UserWithEnrollment]:
    counts = dict(
        db.execute(
            select(Voiceprint.user_id, func.count(Voiceprint.id)).group_by(Voiceprint.user_id)
        ).all()
    )
    seconds = dict(
        db.execute(
            select(Voiceprint.user_id, func.coalesce(func.sum(Voiceprint.duration_seconds), 0.0))
            .group_by(Voiceprint.user_id)
        ).all()
    )
    users = db.execute(select(User).order_by(User.full_name)).scalars().all()
    return [
        UserWithEnrollment(
            **UserOut.model_validate(u).model_dump(),
            voiceprint_count=counts.get(u.id, 0),
            enrolled_seconds=round(float(seconds.get(u.id, 0.0)), 1),
        )
        for u in users
    ]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> User:
    email = payload.email.lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")

    user = User(
        email=email,
        full_name=payload.full_name,
        role=payload.role,
        password_hash=hash_password(payload.password) if payload.password else None,
    )
    db.add(user)
    db.commit()
    return user


@router.post(
    "/{user_id}/voiceprints",
    response_model=VoiceprintOut,
    status_code=status.HTTP_201_CREATED,
)
def enroll_voice(
    user_id: uuid.UUID,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Voiceprint:
    """Enroll one voice sample.

    Three samples of roughly 20 seconds each beats one 60-second sample: it
    captures more of the natural variation in how somebody speaks.
    """
    if not features.is_enabled(db, "voice_enrollment_enabled"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Voice enrollment is turned off. An administrator can enable it "
            "under Users & Voices.",
        )

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        raw = tmpdir / (file.filename or "sample")
        raw.write_bytes(file.file.read())

        try:
            wav = to_wav16k_mono(raw, tmpdir / "sample.wav")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Could not decode audio: {exc}"
            ) from exc

        seconds = duration_seconds(wav)
        if seconds < MIN_ENROLLMENT_SECONDS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Sample is {seconds:.1f}s; at least {MIN_ENROLLMENT_SECONDS:.0f}s "
                "of clean speech is needed for a usable voiceprint",
            )

        embedding = embed_file(wav)
        key = f"voiceprints/{user_id}/{uuid.uuid4()}.wav"
        storage.put_file(key, wav, content_type="audio/wav")

    voiceprint = Voiceprint(
        user_id=user.id,
        embedding=embedding.tolist(),
        sample_key=key,
        duration_seconds=round(seconds, 2),
        origin="enrollment",
    )
    db.add(voiceprint)
    db.commit()
    return voiceprint


@router.get("/{user_id}/voiceprints", response_model=list[VoiceprintOut])
def list_voiceprints(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(current_user),
) -> list[Voiceprint]:
    return list(
        db.execute(
            select(Voiceprint)
            .where(Voiceprint.user_id == user_id)
            .order_by(Voiceprint.created_at.desc())
        ).scalars()
    )


@router.delete("/{user_id}/voiceprints/{voiceprint_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_voiceprint(
    user_id: uuid.UUID,
    voiceprint_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> None:
    voiceprint = db.get(Voiceprint, voiceprint_id)
    if voiceprint is None or voiceprint.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voiceprint not found")
    db.delete(voiceprint)
    db.commit()
