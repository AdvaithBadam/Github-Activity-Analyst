"""Standalone smoke-test for sync_repos().

Proves the repo upsert logic works end-to-end against a real database
and the live GitHub API.  Run it twice in the same invocation to verify
idempotency (no duplicate rows).

Prerequisites:
    1. Apply the migration:  alembic upgrade head
    2. Set TEST_GITHUB_PAT in your .env

Run::

    python scripts/test_sync_repos.py
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

# Ensure the project root is on sys.path so ``app`` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.models.repo import Repo
from app.services.github_client import GitHubClient
from app.services.sync_service import sync_repos

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN = os.getenv("TEST_GITHUB_PAT")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)
if not TOKEN:
    print("ERROR: Set TEST_GITHUB_PAT in your .env file first.")
    print("       Generate one at https://github.com/settings/tokens")
    sys.exit(1)


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    github_client = GitHubClient(access_token=TOKEN)

    # ── Resolve the test user's GitHub username via the API ──────
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            print(f"ERROR: Could not fetch GitHub user (status {resp.status_code})")
            sys.exit(1)
        github_user = resp.json()

    github_username: str = github_user["login"]
    github_created_at = datetime.fromisoformat(
        github_user["created_at"].replace("Z", "+00:00")
    )
    print(f"GitHub user: {github_username}\n")

    # ── Ensure a User row exists for testing ─────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                github_username=github_username,
                github_created_at=github_created_at,
            )
            session.add(user)
            await session.commit()
            print(f"Created test User (id={user.id})")
        else:
            print(f"Found existing User (id={user.id})")

    # ── Run 1: initial sync ──────────────────────────────────────
    print("\n── Run 1: sync_repos ──────────────────────────────────")
    async with session_factory() as session:
        # Re-fetch user inside this session so it's attached
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one()

        repos = await sync_repos(session, user, github_client)

    print(f"Total repos synced: {len(repos)}\n")
    for repo in repos:
        owner_label = "owner" if repo.is_owner else "collaborator"
        print(f"  • {repo.name:<40} ({owner_label})  [github_repo_id={repo.github_repo_id}]")

    # ── Run 2: re-sync to prove idempotency ──────────────────────
    print("\n── Run 2: sync_repos (idempotency check) ──────────────")
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one()

        repos2 = await sync_repos(session, user, github_client)

    print(f"Total repos synced: {len(repos2)}\n")
    for repo in repos2:
        owner_label = "owner" if repo.is_owner else "collaborator"
        print(f"  • {repo.name:<40} ({owner_label})  [github_repo_id={repo.github_repo_id}]")

    # ── Verify no duplicates ─────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(Repo).where(Repo.user_id == user.id)
        )
        all_repos = result.scalars().all()

    print(f"\n── Duplicate check ────────────────────────────────────")
    print(f"Total Repo rows in DB for this user: {len(all_repos)}")
    if len(all_repos) == len(repos2):
        print("✓ No duplicates — upsert is idempotent.")
    else:
        print(f"✗ MISMATCH: synced {len(repos2)} but DB has {len(all_repos)} rows!")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
