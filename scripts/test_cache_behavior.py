"""Cache behavior test for GET /stats/summary.

Tests three scenarios in sequence:
  1. Two hits in a row  -> first: cache_hit=false, second: cache_hit=true, data matches.
  2. Redis unreachable   -> route still returns 200 with correct data, cache_hit=false.
  3. TTL expiry          -> temporarily sets a 5-second TTL, waits for it to lapse,
                            confirms the cache is gone and the route recomputes.

Prerequisites
-------------
1. Redis must be running on localhost:6379 for scenarios 1 and 3.
2. The test data seeded by test_stats_summary.py must already be in the DB
   (or run it first). This script does NOT re-seed -- it uses whatever is live.
3. TEST_GITHUB_PAT set in .env.

Run::

    python scripts/test_cache_behavior.py
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"c:\VS Files\github files\Github-Activity-Analyst")

from dotenv import load_dotenv
load_dotenv(r"c:\VS Files\github files\Github-Activity-Analyst\.env")

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.utils.encryption import encrypt_token
from app.utils.jwt import create_access_token
from app.main import app

DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TEST_GITHUB_PAT")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)
if not TOKEN:
    print("ERROR: TEST_GITHUB_PAT not found in .env")
    sys.exit(1)


async def get_jwt(github_username: str) -> tuple[str, int]:
    """Return a valid JWT and user_id for the test user."""
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one_or_none()
        if user is None:
            print(f"ERROR: No User row for {github_username}. Run test_stats_summary.py first.")
            sys.exit(1)
        user.github_access_token_encrypted = encrypt_token(TOKEN)
        await session.commit()
        user_id = user.id

    await engine.dispose()
    jwt_token = create_access_token(user_id, github_username)
    return jwt_token, user_id


async def hit(transport, jwt_token: str) -> dict:
    """Fire a single GET /stats/summary via ASGI transport and return (status, body)."""
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/stats/summary", cookies={"access_token": jwt_token})
        return resp.status_code, resp.json()


async def flush_cache_key(user_id: int) -> None:
    """Delete the user's cache key from Redis."""
    from app.cache import get_redis_client
    redis = get_redis_client()
    key = f"stats_summary:{user_id}"
    await redis.delete(key)
    print(f"  (Flushed Redis key: {key})")


async def set_short_ttl(user_id: int, ttl_seconds: int) -> None:
    """If a cached value exists, reset its TTL to a short value for expiry testing."""
    from app.cache import get_redis_client
    redis = get_redis_client()
    key = f"stats_summary:{user_id}"
    existing = await redis.get(key)
    if existing:
        await redis.set(key, existing, ex=ttl_seconds)
        print(f"  (Reset TTL on {key} to {ttl_seconds}s)")
    else:
        print(f"  (No cached value at {key} to shorten TTL on)")


async def resolve_github_username() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
        return resp.json()["login"]


def print_response(label: str, status: int, body: dict) -> None:
    print(f"  STATUS CODE : {status}")
    print(f"  cache_hit   : {body.get('cache_hit')}")
    print(f"  current_streak   : {body.get('current_streak')}")
    print(f"  longest_streak   : {body.get('longest_streak')}")
    print(f"  weekly_velocity  : {body.get('weekly_velocity')}")
    print(f"  active_repos     : {body.get('active_repos')}")
    print(f"  computed_at_utc  : {body.get('computed_at_utc')}")


async def main() -> None:
    github_username = await resolve_github_username()
    print(f"GitHub user: {github_username}\n")

    jwt_token, user_id = await get_jwt(github_username)
    print(f"User id={user_id}\n")

    transport = httpx.ASGITransport(app=app)

    # ------------------------------------------------------------------
    # Scenario 1: Two hits in a row
    # ------------------------------------------------------------------
    print("=" * 64)
    print("SCENARIO 1: Two hits in a row")
    print("=" * 64)
    print("  (Flushing any existing cache entry first...)")
    await flush_cache_key(user_id)

    print("\n  [Request 1/2] -- expect cache_hit=false (cold miss)")
    status1, body1 = await hit(transport, jwt_token)
    print_response("Request 1", status1, body1)

    print("\n  [Request 2/2] -- expect cache_hit=true (warm hit, data identical)")
    status2, body2 = await hit(transport, jwt_token)
    print_response("Request 2", status2, body2)

    print("\n  Assertions:")
    assert status1 == 200, f"FAIL: request 1 status {status1}"
    assert status2 == 200, f"FAIL: request 2 status {status2}"
    assert body1.get("cache_hit") is False, f"FAIL: request 1 cache_hit={body1.get('cache_hit')}"
    assert body2.get("cache_hit") is True,  f"FAIL: request 2 cache_hit={body2.get('cache_hit')}"
    # Data must be identical (except cache_hit itself)
    comparable_keys = ["current_streak", "longest_streak", "weekly_velocity", "active_repos", "computed_at_utc"]
    for k in comparable_keys:
        assert body1[k] == body2[k], f"FAIL: field '{k}' differs: {body1[k]} vs {body2[k]}"
    print("  [PASS] status 200 x2")
    print(f"  [PASS] request 1: cache_hit=false")
    print(f"  [PASS] request 2: cache_hit=true")
    print("  [PASS] all metric fields identical between both responses")

    # ------------------------------------------------------------------
    # Scenario 2: Redis unreachable (fail-open)
    # ------------------------------------------------------------------
    print()
    print("=" * 64)
    print("SCENARIO 2: Redis unreachable -- fail-open")
    print("=" * 64)
    print("  Patching REDIS_URL to an unreachable host...")

    # Monkey-patch the singleton to point at a port nothing listens on
    import redis.asyncio as redis_asyncio
    import app.cache as cache_module
    bad_client = redis_asyncio.Redis.from_url(
        "redis://127.0.0.1:19999",   # nothing listening here
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )
    original_client = cache_module._redis_client
    cache_module._redis_client = bad_client

    print("\n  [Request] -- expect 200, cache_hit=false (Redis failure silently skipped)")
    status3, body3 = await hit(transport, jwt_token)
    print_response("Fail-open request", status3, body3)

    print("\n  Assertions:")
    assert status3 == 200, f"FAIL: expected 200, got {status3}"
    assert body3.get("cache_hit") is False, f"FAIL: cache_hit should be false when Redis is down, got {body3.get('cache_hit')}"
    assert body3.get("current_streak") is not None, "FAIL: missing current_streak in response"
    print("  [PASS] status 200 (Redis outage did not cause a 500)")
    print("  [PASS] cache_hit=false")
    print("  [PASS] metrics present in response")

    # Restore the real Redis client
    cache_module._redis_client = original_client
    print("\n  (Restored real Redis client)")

    # ------------------------------------------------------------------
    # Scenario 3: TTL expiry
    # ------------------------------------------------------------------
    short_ttl = 5  # seconds
    print()
    print("=" * 64)
    print(f"SCENARIO 3: TTL expiry (shortened to {short_ttl}s)")
    print("=" * 64)

    # Seed the cache with a warm value by doing a cold request first
    print("  (Flushing cache and doing a fresh request to populate it...)")
    await flush_cache_key(user_id)
    status_seed, body_seed = await hit(transport, jwt_token)
    assert body_seed.get("cache_hit") is False, "FAIL: seed request should be a cache miss"
    print(f"  Seed request: cache_hit={body_seed.get('cache_hit')} (expected false)")

    # Shorten the TTL so we don't wait 300s
    await set_short_ttl(user_id, short_ttl)

    print(f"\n  Verifying cache is still warm (before {short_ttl}s elapses)...")
    status_warm, body_warm = await hit(transport, jwt_token)
    print_response("Pre-expiry request", status_warm, body_warm)
    assert body_warm.get("cache_hit") is True, f"FAIL: expected cache_hit=true before expiry, got {body_warm.get('cache_hit')}"
    print(f"  [PASS] cache_hit=true before TTL expires")

    print(f"\n  Waiting {short_ttl + 1}s for TTL to lapse...")
    time.sleep(short_ttl + 1)

    print("\n  [Request after expiry] -- expect cache_hit=false (recomputed)")
    status_expired, body_expired = await hit(transport, jwt_token)
    print_response("Post-expiry request", status_expired, body_expired)

    print("\n  Assertions:")
    assert status_expired == 200, f"FAIL: expected 200, got {status_expired}"
    assert body_expired.get("cache_hit") is False, f"FAIL: cache_hit should be false after expiry, got {body_expired.get('cache_hit')}"
    print("  [PASS] status 200")
    print("  [PASS] cache_hit=false after TTL expired (recomputed from DB)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print("=" * 64)
    print("  All three scenarios passed.")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
