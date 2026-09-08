from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app import storage
from app.api import auth, meetings, minutes, settings as settings_api, users
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Role, User
from app.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def bootstrap() -> None:
    """Create tables, the storage bucket, and the first admin.

    `create_all` is fine for a scaffold. Before production, switch to Alembic -
    see README "Before production".
    """
    Base.metadata.create_all(bind=engine)

    try:
        storage.ensure_ready()
    except Exception:  # noqa: BLE001 - MinIO may still be starting up
        log.warning("Could not verify object storage", exc_info=True)

    db = SessionLocal()
    try:
        email = settings.admin_email.lower()
        if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is None:
            db.add(
                User(
                    email=email,
                    full_name="Administrator",
                    role=Role.admin,
                    password_hash=hash_password(settings.admin_password),
                )
            )
            db.commit()
            log.info("Created initial admin user %s", email)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    yield


app = FastAPI(
    title="Meeting Minutes",
    description=(
        "Multilingual (English / Hindi / Bengali) meeting transcription with "
        "speaker identification and automatic minutes."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Tighten to your actual frontend origin before deploying.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(meetings.router)
app.include_router(minutes.router)
app.include_router(settings_api.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {
        "status": "ok",
        "asr_provider": settings.asr_provider,
        "minutes_model": settings.anthropic_model,
    }
