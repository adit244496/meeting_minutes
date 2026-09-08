from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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


# Serve the built frontend, if it is there.
#
# Mounted last on purpose: FastAPI matches routes in the order they are added,
# so every /api route and /health above take precedence over this catch-all.
# With no build present (local development, where Vite serves the frontend on
# its own port) this is skipped entirely.
#
# The frontend uses hash routing, so every client-side route is "/#/meetings"
# and the server only ever sees "/". That is why no SPA rewrite rule is needed
# here - an unknown path like /foo returns a plain 404, which is correct.
# Switching the frontend to BrowserRouter would require adding that fallback.
_dist = Path(settings.frontend_dist).resolve() if settings.frontend_dist else None
if _dist and _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
    log.info("Serving frontend from %s", _dist)
else:
    log.info("No frontend build at %s - API only", settings.frontend_dist)
