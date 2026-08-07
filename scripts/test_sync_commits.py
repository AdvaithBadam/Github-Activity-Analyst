"""Standalone smoke-test for sync_commits().

Proves the commit sync logic works end-to-end against a real database
and the live GitHub API.  Runs sync_commits twice in the same invocation
to verify SHA-based dedup (second run should add 0 new commits).

Prerequisites:
    1. Apply all migrations:  alembic upgrade head
    2. Set TEST_GITHUB_PAT in your .env
    3. Run test_sync_repos.py first (User + Repos must already exist)

Run::

    python scripts/test_sync_commits.py
"""

import asyncio
import os
import sys

# Ensure the project root is on sys.path so ``app`` is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.commit import Commit
from app.models.repo import Repo
from app.models.user import User
from app.services.github_client import GitHubClient
from app.services.sync_service import sync_commits

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


def print_results(results: dict[int, int], repo_lookup: dict[int, str]) -> None:
    """Pretty-print the sync_commits return value."""
    total = 0
    for repo_id, count in results.items():
        name = repo_lookup.get(repo_id, f"repo_id={repo_id}")
        print(f"  {name:<50} {count:>5} new commits")
        total += count
    print(f"\n  {'TOTAL':<50} {total:>5} new commits")


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    github_client = GitHubClient(access_token=TOKEN)

    # ── Resolve GitHub username via the API ──────────────────────
    import httpx

    async with httpx.AsyncClient() as http:
        resp = await http.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            print(f"ERROR: Could not fetch GitHub user (status {resp.status_code})")
            sys.exit(1)
        github_username: str = resp.json()["login"]

    print(f"GitHub user: {github_username}\n")

    # ── Load existing User ───────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one_or_none()

    if user is None:
        print("ERROR: No User row found for this github_username.")
        print("       Run test_sync_repos.py first to create the User and Repos.")
        sys.exit(1)

    print(f"Found User (id={user.id})")

    # ── Load existing Repos ──────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(Repo).where(Repo.user_id == user.id)
        )
        repos = list(result.scalars().all())

    if not repos:
        print("ERROR: No Repo rows found for this user.")
        print("       Run test_sync_repos.py first to sync repos.")
        sys.exit(1)

    print(f"Found {len(repos)} repo(s)\n")

    # Build a lookup for pretty printing
    repo_lookup: dict[int, str] = {r.id: r.name for r in repos}

    # ── Run 1: initial commit sync ───────────────────────────────
    print("── Run 1: sync_commits ────────────────────────────────")
    async with session_factory() as session:
        # Re-fetch user and repos inside this session so they're attached
        result = await session.execute(
            select(User).where(User.id == user.id)
        )
        user_attached = result.scalar_one()

        result = await session.execute(
            select(Repo).where(Repo.user_id == user.id)
        )
        repos_attached = list(result.scalars().all())

        results1 = await sync_commits(session, user_attached, github_client, repos_attached)

    print_results(results1, repo_lookup)

    # ── Run 2: idempotency check ─────────────────────────────────
    print("\n── Run 2: sync_commits (idempotency check) ────────────")
    async with session_factory() as session:
        result = await session.execute(
            select(User).where(User.id == user.id)
        )
        user_attached = result.scalar_one()

        result = await session.execute(
            select(Repo).where(Repo.user_id == user.id)
        )
        repos_attached = list(result.scalars().all())

        results2 = await sync_commits(session, user_attached, github_client, repos_attached)

    print_results(results2, repo_lookup)

    # ── Verify: all repos should show 0 new commits on run 2 ────
    all_zero = all(count == 0 for count in results2.values())
    print()
    if all_zero:
        print("✓ Idempotent — run 2 added 0 new commits (SHA dedup works).")
    else:
        print("✗ UNEXPECTED — run 2 added new commits! Check dedup logic.")

    # ── Final: total commits in DB ───────────────────────────────
    async with session_factory() as session:
        result = await session.execute(select(func.count(Commit.id)))
        total_commits = result.scalar_one()

    print(f"\nTotal commits in DB: {total_commits}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
