"""Asking a running background job to stop.

Celery can revoke a task that has not started yet, but a task already running
in a worker can only be stopped by killing the process - which would take
whatever else that worker is doing with it. So this is cooperative: the API
raises a flag, and the job looks at it between units of work and gives up.

Redis rather than the database because the worker holds no session across a
long model call, and because the flag has to be visible from another process
the instant it is set.
"""

from __future__ import annotations

import logging

import redis

from app.config import settings

log = logging.getLogger(__name__)

# Long enough to outlive any job it could apply to, short enough that a flag
# nobody consumed cannot stop a translation started days later.
TTL_SECONDS = 2 * 60 * 60


def _key(meeting_id: str, job: str) -> str:
    return f"meeting:{meeting_id}:{job}:cancel"


def _task_key(meeting_id: str, job: str) -> str:
    return f"meeting:{meeting_id}:{job}:task"


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url)


def request(meeting_id: str, job: str, task_id: str | None) -> None:
    """Ask a specific run of the job to stop at its next checkpoint.

    The flag names the task it applies to, because a cancellation can outlive
    what it was meant to stop: a job cancelled while still queued may be
    revoked and never run, leaving the flag behind. A flag naming a task that
    is no longer running is inert, so the next translation is unaffected.
    """
    try:
        _client().set(_key(meeting_id, job), task_id or ANY, ex=TTL_SECONDS)
    except redis.RedisError:
        log.warning("Could not request cancellation of %s for %s", job, meeting_id, exc_info=True)


# Used only when the task id could not be recorded - Redis dropped it, or the
# queue call failed halfway. Rare, and the job clears the flag as it ends.
ANY = "*"


def requested(meeting_id: str, job: str, task_id: str | None = None) -> bool:
    """Has this run been asked to stop?"""
    try:
        raw = _client().get(_key(meeting_id, job))
    except redis.RedisError:
        # Never stop a job because Redis hiccuped - the job finishing is the
        # safer failure here.
        return False
    if not raw:
        return False
    flagged = raw.decode() if isinstance(raw, bytes) else str(raw)
    return flagged in (ANY, task_id) if task_id else True


def clear(meeting_id: str, job: str) -> None:
    """Drop a stale flag. Every job calls this as it starts, so a cancellation
    nobody acted on cannot kill the next attempt."""
    try:
        _client().delete(_key(meeting_id, job))
    except redis.RedisError:
        log.warning("Could not clear the %s cancel flag for %s", job, meeting_id, exc_info=True)


def remember_task(meeting_id: str, job: str, task_id: str) -> None:
    """Note which Celery task is doing this, so it can be revoked while queued."""
    try:
        _client().set(_task_key(meeting_id, job), task_id, ex=TTL_SECONDS)
    except redis.RedisError:
        log.warning("Could not record the %s task id for %s", job, meeting_id, exc_info=True)


def task_id(meeting_id: str, job: str) -> str | None:
    try:
        raw = _client().get(_task_key(meeting_id, job))
    except redis.RedisError:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw
