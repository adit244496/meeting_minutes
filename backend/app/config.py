from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Built frontend. When this directory exists the API serves it directly, so
    # the app works on a bare IP with no nginx in front. Relative to the backend
    # working directory. Empty string disables it.
    frontend_dist: str = "../frontend/dist"

    secret_key: str = "dev-secret-change-me"
    admin_email: str = "admin@example.com"
    admin_password: str = "changeme123"
    access_token_ttl_minutes: int = 60 * 12

    database_url: str = "postgresql+psycopg://mm:mm@db:5432/meeting_minutes"
    redis_url: str = "redis://redis:6379/0"

    # local | s3.  Local keeps recordings on disk with no external dependency.
    storage_backend: str = "local"
    local_storage_dir: str = "/data/recordings"

    # Recordings are deleted after this many days. Transcripts and minutes are
    # never touched - see the retention task in app/worker/tasks.py.
    recording_retention_days: int = 7

    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "meeting-audio"
    s3_region: str = "us-east-1"

    # Gemini by default: measured best on code-switched English/Hindi/Bengali,
    # which is the dominant mode in these meetings.
    asr_provider: str = "gemini"
    elevenlabs_api_key: str = ""
    sarvam_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    minutes_language: str = "en"

    # Phase flags. Both off = transcript only, which is the right place to
    # start: it isolates ASR quality from everything built on top of it.
    auto_identify_speakers: bool = False
    auto_generate_minutes: bool = False

    speaker_match_threshold: float = 0.35
    speaker_embed_seconds: int = 30


    @field_validator("database_url")
    @classmethod
    def _require_psycopg3(cls, value: str) -> str:
        """Force the psycopg 3 driver.

        A bare `postgresql://` makes SQLAlchemy reach for psycopg2, which is not
        installed - and the failure is an opaque `ModuleNotFoundError: No module
        named 'psycopg2'` rather than anything about the URL. Everyone writes the
        bare form from memory, so rewrite it instead of failing.
        """
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
