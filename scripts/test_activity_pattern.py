"""Smoke-test for GET /stats/activity-pattern.

Hits the route via httpx.ASGITransport (no running server needed) and prints
the full by_hour_utc and by_day_of_week histograms exactly as the API returns
them, plus a set of structural assertions.

This test deliberately does NOT seed synthetic commits — it operates on whatever
real Commit rows exist in the live dev DB for the test user.  That gives you a
realistic picture of the actual hour/DoW distribution and lets you confirm that
committed_at values are being extracted in UTC (not the Postgres session tz).

Prerequisites
-------------
1. alembic upgrade head
2. TEST_GITHUB_PAT set in .env
3. At least one Commit row in the DB for the test user (run test_sync_commits.py
   first if the DB is empty).

Run::

    python scripts/test_activity_pattern.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.utils.encryption import encrypt_token
from app.utils.jwt import create_access_token
from app.main import app as fastapi_app


DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TEST_GITHUB_PAT")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)
if not TOKEN:
    print("ERROR: TEST_GITHUB_PAT not found in .env")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────


async def resolve_github_user() -> tuple[str, datetime]:
    """Return (github_username, github_created_at) from the GitHub API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code != 200:
        print(f"ERROR: GitHub API returned {resp.status_code}")
        sys.exit(1)
    data = resp.json()
    created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
    return data["login"], created_at


async def get_or_create_user(
    session_factory: async_sessionmaker,
    github_username: str,
    github_created_at: datetime,
) -> User:
    """Get (or create) a User row and refresh its encrypted token."""
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(
                github_username=github_username,
                github_created_at=github_created_at,
                github_access_token_encrypted=encrypt_token(TOKEN),
            )
            session.add(user)
        else:
            user.github_access_token_encrypted = encrypt_token(TOKEN)
        await session.commit()
        return user


# ── Main ──────────────────────────────────────────────────────────


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Resolve / create user
    github_username, github_created_at = await resolve_github_user()
    print(f"GitHub user: {github_username}")

    user = await get_or_create_user(session_factory, github_username, github_created_at)
    print(f"User id={user.id}\n")

    # ── Hit the route ────────────────────────────────────────────────────────
    jwt_token = create_access_token(user.id, user.github_username)
    transport = httpx.ASGITransport(app=fastapi_app)

    print("-- GET /stats/activity-pattern ----------------------------------------")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/stats/activity-pattern",
            cookies={"access_token": jwt_token},
        )

    print(f"STATUS CODE: {resp.status_code}")
    print()

    if resp.status_code != 200:
        print(f"BODY: {resp.text}")
        sys.exit(1)

    body = resp.json()

    # ── Print by_hour_utc ────────────────────────────────────────────────────
    print("-- by_hour_utc (UTC, 24-slot histogram) -------------------------------")
    by_hour: list[dict] = body["by_hour_utc"]
    max_count = max((b["commit_count"] for b in by_hour), default=0) or 1
    bar_width = 40
    for bucket in by_hour:
        h = bucket["hour"]
        c = bucket["commit_count"]
        bar = "█" * int(bar_width * c / max_count)
        print(f"  {h:02d}:00  {bar:<{bar_width}}  {c}")

    print()
    total_hour_commits = sum(b["commit_count"] for b in by_hour)
    print(f"  Total commits across all hours: {total_hour_commits}")
    print(f"  Hours with data: {sum(1 for b in by_hour if b['commit_count'] > 0)} / 24")
    peak_hour = max(by_hour, key=lambda b: b["commit_count"])
    print(f"  Peak hour (UTC): {peak_hour['hour']:02d}:00  ({peak_hour['commit_count']} commits)")
    print()

    # ── Print by_day_of_week ─────────────────────────────────────────────────
    print("-- by_day_of_week (ISO Monday=1 … Sunday=7) ---------------------------")
    by_dow: list[dict] = body["by_day_of_week"]
    max_dow = max((b["commit_count"] for b in by_dow), default=0) or 1
    for bucket in by_dow:
        day = bucket["day"]
        c = bucket["commit_count"]
        bar = "█" * int(bar_width * c / max_dow)
        print(f"  {day:<10}  {bar:<{bar_width}}  {c}")

    print()
    total_dow_commits = sum(b["commit_count"] for b in by_dow)
    print(f"  Total commits across all days: {total_dow_commits}")
    peak_dow = max(by_dow, key=lambda b: b["commit_count"])
    print(f"  Peak day: {peak_dow['day']}  ({peak_dow['commit_count']} commits)")
    print()

    # ── Structural assertions ────────────────────────────────────────────────
    print("-- Structural assertions ----------------------------------------------")

    assert resp.status_code == 200, f"FAIL: expected 200, got {resp.status_code}"
    print("  [PASS] status 200")

    assert len(by_hour) == 24, f"FAIL: by_hour_utc should have 24 entries, got {len(by_hour)}"
    print(f"  [PASS] by_hour_utc has exactly 24 slots")

    assert len(by_dow) == 7, f"FAIL: by_day_of_week should have 7 entries, got {len(by_dow)}"
    print(f"  [PASS] by_day_of_week has exactly 7 slots")

    expected_hours = list(range(24))
    actual_hours = [b["hour"] for b in by_hour]
    assert actual_hours == expected_hours, \
        f"FAIL: hours out of order or missing. Expected {expected_hours}, got {actual_hours}"
    print("  [PASS] by_hour_utc hours 0–23 in order, none missing")

    expected_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    actual_days = [b["day"] for b in by_dow]
    assert actual_days == expected_days, \
        f"FAIL: days out of order or missing.\n  Expected: {expected_days}\n  Got:      {actual_days}"
    print("  [PASS] by_day_of_week Mon–Sun in order, none missing")

    # Hour/DoW totals should agree (both count all commits for the user).
    assert total_hour_commits == total_dow_commits, (
        f"FAIL: hour total ({total_hour_commits}) != DoW total ({total_dow_commits}). "
        "Indicates GROUP BY mismatch or data inconsistency."
    )
    print(f"  [PASS] hour total == dow total ({total_hour_commits} commits — sums consistent)")

    assert "computed_at_utc" in body, "FAIL: missing computed_at_utc field"
    print(f"  [PASS] computed_at_utc = {body['computed_at_utc']}")

    print()
    print("=" * 60)
    print("  All assertions passed.")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
