"""Job progress over Redis pub/sub, surfaced to the browser as SSE.

Post-meeting processing takes minutes, so the UI needs to say more than
"loading". The worker publishes to a per-meeting channel; the API subscribes and
relays. SSE rather than WebSockets deliberately - this stream is one-directional
and SSE reconnects on its own.
"""

from __future__ import annotations

import json
import logging

import redis

from app.config import settings

log = logging.getLogger(__name__)

# Last known state, so a browser that connects mid-job sees progress immediately
# instead of waiting for the next event.
STATE_TTL_SECONDS = 24 * 60 * 60


def channel(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:progress"


def state_key(meeting_id: str) -> str:
    return f"meeting:{meeting_id}:state"


def publish(meeting_id: str, stage: str, percent: int, message: str = "") -> None:
    payload = json.dumps(
        {"meeting_id": str(meeting_id), "stage": stage, "percent": percent, "message": message}
    )
    try:
        client = redis.Redis.from_url(settings.redis_url)
        client.set(state_key(meeting_id), payload, ex=STATE_TTL_SECONDS)
        client.publish(channel(meeting_id), payload)
    except redis.RedisError:
        # Progress reporting must never take down the job it is reporting on.
        log.warning("Could not publish progress for meeting %s", meeting_id, exc_info=True)


def last_state(meeting_id: str) -> dict | None:
    try:
        raw = redis.Redis.from_url(settings.redis_url).get(state_key(meeting_id))
    except redis.RedisError:
        return None
    return json.loads(raw) if raw else None
