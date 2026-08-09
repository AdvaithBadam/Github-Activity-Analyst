"""Standalone smoke-test for GET /stats/summary.

Seeds a known set of DailySnapshot and Commit rows into the DB so the
response values are deterministic, hits the route via httpx.ASGITransport,
then prints the status code and JSON body.

Known-data scenario seeded
--------------------------
* 5 consecutive active days ending yesterday (UTC), giving a current_streak
  of 5 and a longest_streak of at least 5.
* 2 extra active days seeded 30 days ago, so the longest streak is NOT
  extended by them (they are isolated).
* 3 active days in the last 7 days (inside the weekly window), so
  weekly_velocity is the sum of their commit counts.
* 2 distinct repos have commits within the last 14 days, so active_repos == 2.

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

sys.path.insert(0, r"c:\VS Files\github files\Github-Activity-Analyst")

from dotenv import load_dotenv
load_dotenv(r"c:\VS Files\github files\Github-Activity-Analyst\.env")

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

    Scenario
    --------
    DailySnapshot rows seeded (all existing rows for this user are wiped first):
        - today-5, today-4, today-3, today-2, today-1 (yesterday)
          commit_counts: 1, 1, 2, 3, 4  → 5-day streak ending yesterday
        - today is NOT seeded → streak ends yesterday (still 5)
        - 30 days ago  →  isolated day (longest stays 5, not extended)

    weekly_velocity (last 7 days = date >= today-7):
        Only today-5 through today-1 fall in the window.
        Sum: 1+1+2+3+4 = 11  (today-6 and today-7 have no snapshot row)

    active_repos:
        NOTE: this metric counts ALL repos for this user with commits in the
        last 14 days, including real repos already synced from GitHub.  The
        seed inserts 2 extra test repos (ids 15, 16) but previously-synced
        repos with recent commits are also counted.  The assertion below
        checks >= 2 rather than == 2 to accommodate real data.

    Commit rows (for active_repos):
        - 2 test repos each with 1 commit in the last 14 days
    """
    yesterday = today - timedelta(days=1)

    # Days in the 5-day streak: today-5 through today-1 (yesterday)
    streak_days = [today - timedelta(days=i) for i in range(1, 6)]
    streak_counts = {
        today - timedelta(days=1): 4,  # yesterday
        today - timedelta(days=2): 3,
        today - timedelta(days=3): 2,
        today - timedelta(days=4): 1,
        today - timedelta(days=5): 1,
    }
    isolated_day = today - timedelta(days=30)

    async with session_factory() as session:
        # ── Clear existing snapshot rows for this user ────────────
        await session.execute(
            delete(DailySnapshot).where(DailySnapshot.user_id == user.id)
        )

        # ── Seed DailySnapshot rows ───────────────────────────────
        for d, count in streak_counts.items():
            session.add(DailySnapshot(user_id=user.id, date=d, commit_count=count))

        # Isolated day 30 days ago (longest streak stays at 5)
        session.add(DailySnapshot(user_id=user.id, date=isolated_day, commit_count=7))

        await session.commit()

    # ── Ensure 2 test repos exist for the active_repos metric ────
    async with session_factory() as session:
        test_repo_ids: list[int] = []
        for i in range(1, 3):
            fake_github_repo_id = -(user.id * 100 + i)  # negative to avoid clashes
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

        # Clear old test commits for these repos
        for rid in test_repo_ids:
            await session.execute(delete(Commit).where(Commit.repo_id == rid))

        # Insert 1 commit in last 14 days per test repo
        for idx, rid in enumerate(test_repo_ids):
            session.add(
                Commit(
                    repo_id=rid,
                    sha=f"deadbeef0{idx}0000000000000000000000000000000",
                    message=f"test commit for repo {idx+1}",
                    committed_at=datetime.now(timezone.utc) - timedelta(days=idx + 1),
                )
            )

        await session.commit()
        print(f"Seeded test repos: {[f'id={rid}' for rid in test_repo_ids]}")


async def main() -> None:
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()

    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # ── Resolve GitHub username ───────────────────────────────────
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

    # ── Ensure user row exists ────────────────────────────────────
    user = await get_or_create_user(session_factory, github_username, github_created_at)
    print(f"User id={user.id}\n")

    # ── Seed deterministic test data ──────────────────────────────
    print("Seeding test data...")
    await seed_test_data(session_factory, user, today)
    print("Expected metrics:")
    print("  current_streak   = 5   (5-day streak ending yesterday)")
    print("  longest_streak   = 5   (isolated day 30d ago doesn't extend it)")
    print("  weekly_velocity  = 11  (today-5..today-1 in window: 1+1+2+3+4)")
    print("  active_repos     >= 2  (2 seeded test repos + any real synced repos in last 14d)")
    print()

    # ── Issue JWT ─────────────────────────────────────────────────
    jwt_token = create_access_token(user.id, github_username)

    # ── Hit GET /stats/summary ────────────────────────────────────
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/stats/summary", cookies={"access_token": jwt_token})

    print(f"STATUS CODE: {resp.status_code}")
    print("JSON BODY:")
    print(resp.text)

    import json
    body = resp.json()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert body["current_streak"] == 5, f"current_streak: expected 5, got {body['current_streak']}"
    assert body["longest_streak"] == 5, f"longest_streak: expected 5, got {body['longest_streak']}"
    assert body["weekly_velocity"] == 11, f"weekly_velocity: expected 11, got {body['weekly_velocity']}"
    assert body["active_repos"] >= 2, f"active_repos: expected >= 2, got {body['active_repos']}"
    assert "computed_at_utc" in body, "missing computed_at_utc field"
    print("\n[PASS] All assertions passed.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
