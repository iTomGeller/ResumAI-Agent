from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# TTLs per cache point (plan §J2). Keys are full content hashes — cache
# entries can never leak across candidates or JDs.
TTL_PARSE_RESUME = 30 * 24 * 3600
TTL_JD_ANALYSIS = 7 * 24 * 3600
TTL_PLAN = 24 * 3600
TTL_QUERY_REWRITE = 24 * 3600

_redis = None
_redis_failed = False


def content_key(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"resumai:cache:{namespace}:{digest}"


async def _client():
    """Lazy aioredis client; a failed connection disables the cache for the
    process lifetime (cache misses must never break the run)."""
    global _redis, _redis_failed
    if not settings.cache_enabled or _redis_failed:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=3, socket_timeout=3)
        await _redis.ping()
        return _redis
    except Exception as exc:  # noqa: BLE001 - cache is strictly best-effort
        logger.info("semantic cache disabled (redis unavailable): %s", exc)
        _redis_failed = True
        _redis = None
        return None


async def get_json(key: str) -> Optional[Any]:
    client = await _client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache get failed %s: %s", key, exc)
        return None


async def set_json(key: str, value: Any, ttl_seconds: int) -> None:
    client = await _client()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.debug("cache set failed %s: %s", key, exc)


async def get_or_compute(key: str, ttl_seconds: int,
                         compute: Callable[[], Awaitable[Any]]) -> tuple[Any, bool]:
    """Returns (value, cache_hit). compute() runs only on miss; its result is
    stored unless it is None/empty."""
    cached = await get_json(key)
    if cached is not None:
        return cached, True
    value = await compute()
    if value:
        await set_json(key, value, ttl_seconds)
    return value, False
