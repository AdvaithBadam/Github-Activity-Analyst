"""Async Redis client — thin wrapper following the same pattern as app/db.py.

Usage in route handlers::

    from app.cache import get_redis_client, invalidate_stats_cache

    redis = get_redis_client()
    value = await redis.get("some_key")

The client is a module-level singleton created from ``settings.REDIS_URL``.
All I/O is non-blocking via ``redis.asyncio``.

Important: callers must handle ``redis.asyncio.RedisError`` (and its base
class ``Exception``) themselves.  This module intentionally does NOT swallow
errors — it's the caller's responsibility to decide how to handle them (e.g.
fail-open on cache miss, log a warning, etc.)
"""

import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

# All cache key patterns used by app/api/stats.py.  Must stay in sync with the
# constants declared in that module.
_STATS_CACHE_KEYS: tuple[str, ...] = (
    "stats_summary:{user_id}",
    "stats_heatmap:{user_id}",
    "stats_repos:{user_id}",
    "stats_activity_pattern:{user_id}",
)

# Module-level singleton — created once at import time, reused across requests.
# redis.asyncio.Redis uses a connection pool internally so this is safe.
_redis_client: Redis = Redis.from_url(
    settings.REDIS_URL,
    encoding="utf-8",
    decode_responses=True,  # always return str, not bytes
)


def get_redis_client() -> Redis:
    """Return the shared async Redis client.

    Returns the same singleton on every call — no I/O happens here.
    The actual connection is made lazily on the first command.
    """
    return _redis_client


async def invalidate_stats_cache(user_id: int) -> None:
    """Delete all stats cache entries for *user_id* in one Redis DEL call.

    Covers: stats_summary, stats_heatmap, stats_repos, stats_activity_pattern.

    Fail-open: a Redis error is logged as a warning but never re-raised.
    A failed invalidation must never break the caller (POST /sync/github).
    """
    keys = [pattern.format(user_id=user_id) for pattern in _STATS_CACHE_KEYS]
    try:
        await get_redis_client().delete(*keys)
        logger.info("invalidate_stats_cache: deleted keys %s", keys)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "invalidate_stats_cache: failed to delete keys %s — cache may be stale: %s",
            keys,
            exc,
        )

