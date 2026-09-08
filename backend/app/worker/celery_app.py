from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery = Celery(
    "meeting_minutes",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # A 3-hour meeting through a hosted ASR plus embedding plus minutes can run
    # long; a short default would kill jobs mid-flight.
    task_time_limit=3 * 60 * 60,
    task_soft_time_limit=3 * 60 * 60 - 300,
    result_expires=7 * 24 * 60 * 60,
)

# Retention sweep. Runs daily rather than continuously - "older than 7 days"
# does not need minute-level precision, and a daily job is far easier to reason
# about when someone asks why a recording disappeared.
celery.conf.beat_schedule = {
    "purge-old-recordings": {
        "task": "recordings.purge",
        "schedule": crontab(hour=3, minute=30),
    },
}
