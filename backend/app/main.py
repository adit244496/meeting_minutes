from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app import storage
from app.api import auth, meetings, minutes, settings as settings_api, users
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Role, User
from app.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _require_pgvector() -> None:
    """Fail with one readable line instead of a DDL traceback.

    pgvector has to be enabled per database, not just installed on the server -
    a distinction that otherwise surfaces as `type "vector" does not exist`
    buried under sixty lines of SQLAlchemy stack, halfway through CREATE TABLE.
    The app cannot create it itself: `vector` is not a trusted extension, so it
    needs a superuser.
    """
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()

    if not found:
        db = engine.url.database
        raise RuntimeError(
            f"The pgvector extension is not enabled in the '{db}' database. "
            "Enable it as a superuser:  "
            f"sudo -u postgres psql -d {db} -c 'CREATE EXTENSION IF NOT EXISTS vector;'  "
            "Installing the package on the server is not enough - the extension "
            "is enabled per database."
        )


def _check_ffmpeg() -> None:
    """Warn loudly at startup if ffmpeg is missing.

    Every meeting is transcoded before anything else happens, so without ffmpeg
    nothing can process - but the failure otherwise surfaces only when somebody
    uploads their first recording, as a job that fails deep in the worker. A
    warning here puts it in the startup log instead. Not fatal: the UI and the
    existing transcripts stay usable.
    """
    import shutil

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    log.warning(
        "ffmpeg/ffprobe not found on PATH. Every meeting is transcoded before "
        "transcription, so uploads will fail until it is installed:  "
        "sudo apt install -y ffmpeg"
    )


def bootstrap() -> None:
    """Create tables, the storage bucket, and the first admin.

    `create_all` is fine for a scaffold. Before production, switch to Alembic -
    see README "Before production".
    """
    _check_ffmpeg()
    _require_pgvector()
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
# Mounting StaticFiles at "/" looks simpler but is wrong: a mount full-matches
# ANY path, including "/api/meetings/", which beats FastAPI's trailing-slash
# redirect and returns a 404 from the static handler instead. Assets get their
# own mount and everything else falls through to an explicit catch-all that
# knows about /api.
_dist = Path(settings.frontend_dist).resolve() if settings.frontend_dist else None

if _dist and _dist.is_dir():
    _index = _dist / "index.html"

    if (_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_frontend(full_path: str):
        # Registered after every router, so real API routes already won. Only
        # unmatched paths reach here.
        if full_path == "api" or full_path.startswith("api/"):
            if full_path.endswith("/"):
                # What FastAPI would have done unaided: send the client to the
                # canonical path. 307 keeps the method and body intact.
                return RedirectResponse("/" + full_path.rstrip("/"), status_code=307)
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such endpoint")

        candidate = _dist / full_path
        if full_path and candidate.is_file() and _dist in candidate.resolve().parents:
            return FileResponse(candidate)

        return FileResponse(_index)

    log.info("Serving frontend from %s", _dist)
else:
    log.info("No frontend build at %s - API only", settings.frontend_dist)
