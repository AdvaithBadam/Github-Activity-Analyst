"""Standalone smoke-test for GET /stats/summary.

Seeds a known set of DailySnapshot rows into the DB so the response values
are deterministic, hits the route via httpx.ASGITransport, then prints the
status code and JSON body and asserts all expected values.

Boundary test for weekly_velocity
----------------------------------
The route sets the window cutoff at ``now - timedelta(days=6)``, meaning the
query is ``DailySnapshot.date >= cutoff.date()``  ->  7 calendar days
inclusive of today (today-6 through today).

  today-6  ->  IN  window  (date >= today-6  true)
  today-7  ->  OUT of window  (date < today-6   false)

Both days are seeded with 1 commit so the boundary effect is unambiguous:
  weekly_velocity = 1 (today-6) + 1 (today-5) + 1 (today-4) + 2 (today-3)
                  + 3 (today-2) + 4 (today-1) = 12
  today-7's commit count (1) is NOT added -> if it were included, the total
  would be 13.

Seeded DailySnapshot rows (all existing rows for this user are wiped first)
----------------------------------------------------------------------------
  today-7  ->  1 commit   (OUT of weekly window, IN streak)
  today-6  ->  1 commit   (IN  weekly window, IN streak)
  today-5  ->  1 commit   (IN  weekly window, IN streak)
  today-4  ->  1 commit   (IN  weekly window, IN streak)
  today-3  ->  2 commits  (IN  weekly window, IN streak)
  today-2  ->  3 commits  (IN  weekly window, IN streak)
  today-1  ->  4 commits  (IN  weekly window, IN streak)
  today    ->  NOT seeded  (streak ends yesterday, streak counts from today-7)
  today-30 ->  7 commits  (isolated, OUT of weekly window, does NOT extend streak)

Expected metrics
----------------
  current_streak  = 7   (today-7 through today-1 are consecutive; today missing)
  longest_streak  = 7   (same run is the longest)
  weekly_velocity = 12  (sum of today-6..today-1: 1+1+1+2+3+4=12; today-7 excluded)
  active_repos    >= 2  (2 seeded test repos with recent commits, plus any
                         real synced repos in the last 14 days)

Prerequisites
-------------
1. alembic upgrade head
2. TEST_GITHUB_PAT set in .env (used to get/create the test user)

Run::

    python scripts/test_stats_summary.py
"""

import asyncio
import os
import sys
from datetime import datetime, date, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.models.repo import Repo
from app.models.commit import Commit
from app.models.daily_snapshot import DailySnapshot
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


async def get_or_create_user(
    session_factory: async_sessionmaker,
    github_username: str,
    github_created_at: datetime,
) -> User:
    """Get or create a User row; always refreshes the encrypted token."""
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


async def seed_test_data(
    session_factory: async_sessionmaker,
    user: User,
    today: date,
) -> None:
    """Wipe and re-seed DailySnapshot + Commit rows for a deterministic scenario.

    See module docstring for the full scenario description.
    """
    snapshot_counts = {
        today - timedelta(days=7): 1,   # OUT of weekly window -- boundary test
        today - timedelta(days=6): 1,   # IN  weekly window -- boundary test
        today - timedelta(days=5): 1,
        today - timedelta(days=4): 1,
        today - timedelta(days=3): 2,
        today - timedelta(days=2): 3,
        today - timedelta(days=1): 4,   # yesterday
        # today: NOT seeded (streak ends at yesterday)
        today - timedelta(days=30): 7,  # isolated, out of window
    }

    async with session_factory() as session:
        await session.execute(
            delete(DailySnapshot).where(DailySnapshot.user_id == user.id)
        )
        for d, count in snapshot_counts.items():
            session.add(DailySnapshot(user_id=user.id, date=d, commit_count=count))
        await session.commit()

    print(f"Seeded {len(snapshot_counts)} DailySnapshot rows:")
    for d in sorted(snapshot_counts):
        offset = (today - d).days
        in_window = d >= today - timedelta(days=6)
        window_label = "IN  weekly window" if in_window else "OUT of weekly window"
        print(f"  today-{offset:2d}  ({d})  count={snapshot_counts[d]}  [{window_label}]")

    async with session_factory() as session:
        test_repo_ids: list[int] = []
        for i in range(1, 3):
            fake_github_repo_id = -(user.id * 100 + i)
            result = await session.execute(
                select(Repo).where(
                    Repo.user_id == user.id,
                    Repo.github_repo_id == fake_github_repo_id,
                )
            )
            repo = result.scalar_one_or_none()
            if repo is None:
                repo = Repo(
                    user_id=user.id,
                    github_repo_id=fake_github_repo_id,
                    name=f"test-repo-{i}",
                    owner_login=user.github_username,
                    is_owner=True,
                    github_created_at=datetime.now(timezone.utc),
                )
                session.add(repo)
                await session.flush()
            test_repo_ids.append(repo.id)

        for rid in test_repo_ids:
            await session.execute(delete(Commit).where(Commit.repo_id == rid))

        for idx, rid in enumerate(test_repo_ids):
            session.add(
                Commit(
                    repo_id=rid,
                    sha=f"deadbeef0{idx}0000000000000000000000000000000",
                    message=f"test commit for repo {idx + 1}",
                    committed_at=datetime.now(timezone.utc) - timedelta(days=idx + 1),
                )
            )

        await session.commit()
        print(f"\nSeeded test repos: {[f'id={rid}' for rid in test_repo_ids]}")


async def main() -> None:
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

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
        github_user = resp.json()

    github_username: str = github_user["login"]
    github_created_at = datetime.fromisoformat(
        github_user["created_at"].replace("Z", "+00:00")
    )
    print(f"GitHub user: {github_username}")

    user = await get_or_create_user(session_factory, github_username, github_created_at)
    print(f"User id={user.id}\n")

    print("-- Seeding test data --------------------------------------------------")
    await seed_test_data(session_factory, user, today)

    print()
    print("-- Expected metrics ---------------------------------------------------")
    print("  current_streak  = 7   (today-7 .. today-1 consecutive; today missing)")
    print("  longest_streak  = 7   (same run)")
    print("  weekly_velocity = 12  (today-6..today-1 in window: 1+1+1+2+3+4;")
    print("                         today-7 count=1 is OUT of window -> NOT added;")
    print("                         if included it would be 13)")
    print("  active_repos    >= 2  (2 seeded test repos + any synced repos in last 14d)")
    print()

    jwt_token = create_access_token(user.id, github_username)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/stats/summary", cookies={"access_token": jwt_token})

    print("-- Actual response ----------------------------------------------------")
    print(f"STATUS CODE: {resp.status_code}")
    print(f"JSON BODY:   {resp.text}")
    print()

    print("-- Assertions ---------------------------------------------------------")
    body = resp.json()

    assert resp.status_code == 200, \
        f"FAIL: expected 200, got {resp.status_code}"
    print("  [PASS] status 200")

    assert body["current_streak"] == 7, \
        f"FAIL: current_streak expected 7, got {body['current_streak']}"
    print(f"  [PASS] current_streak = {body['current_streak']}")

    assert body["longest_streak"] == 7, \
        f"FAIL: longest_streak expected 7, got {body['longest_streak']}"
    print(f"  [PASS] longest_streak = {body['longest_streak']}")

    assert body["weekly_velocity"] == 12, \
        (
            f"FAIL: weekly_velocity expected 12 "
            f"(today-6 IN window, today-7 OUT of window), "
            f"got {body['weekly_velocity']}"
        )
    print(f"  [PASS] weekly_velocity = {body['weekly_velocity']}  (today-6 IN, today-7 OUT -> 12 not 13)")

    assert body["active_repos"] >= 2, \
        f"FAIL: active_repos expected >= 2, got {body['active_repos']}"
    print(f"  [PASS] active_repos = {body['active_repos']}  (>= 2)")

    assert "computed_at_utc" in body, "FAIL: missing computed_at_utc field"
    print(f"  [PASS] computed_at_utc = {body['computed_at_utc']}")

    print()
    print("=" * 60)
    print("  All assertions passed.")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
