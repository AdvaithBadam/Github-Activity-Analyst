"""One-off cleanup script — removes synthetic ``test-repo-*`` data from the live DB.

Earlier test scripts (e.g. test_stats_summary.py) inserted Repo rows whose
name starts with ``test-repo`` and committed real Commit rows against them.
These synthetic rows pollute /stats/repos and /stats/summary responses for the
real dev user.

What this script does
---------------------
1. Resolves the real test user via TEST_GITHUB_PAT (same user identity used by
   other test scripts).
2. Finds all Repo rows for that user whose ``name`` starts with ``test-repo``.
3. Deletes all Commit rows whose ``repo_id`` is in that set (cascade-safe even
   though the FK already has cascade delete, we do it explicitly so we can
   count what was removed).
4. Deletes the Repo rows themselves.
5. Prints a before/after /stats/repos comparison so you can confirm the rows
   are gone.

Safety
------
- Only touches rows matching the ``test-repo%`` prefix — real repos are never
  touched.
- Dry-run mode (``--dry-run``) prints what *would* be deleted without touching
  the DB.

Prerequisites
-------------
1. alembic upgrade head
2. TEST_GITHUB_PAT set in .env (same as other test scripts)

Run::

    python scripts/cleanup_test_repos.py
    python scripts/cleanup_test_repos.py --dry-run
    python scripts/cleanup_test_repos.py --skip-api-check
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.commit import Commit
from app.models.repo import Repo
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


async def fetch_stats_repos(user: User) -> list[dict]:
    """Hit GET /stats/repos via ASGITransport and return the repos list."""
    jwt_token = create_access_token(user.id, user.github_username)
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/stats/repos", cookies={"access_token": jwt_token})
    if resp.status_code != 200:
        print(f"  WARNING: /stats/repos returned {resp.status_code}: {resp.text}")
        return []
    return resp.json().get("repos", [])


# ── Main ──────────────────────────────────────────────────────────


async def main(dry_run: bool, skip_api_check: bool) -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # ── Resolve real user ────────────────────────────────────────────────────
    github_username, github_created_at = await resolve_github_user()
    print(f"GitHub user: {github_username}")

    user = await get_or_create_user(session_factory, github_username, github_created_at)
    print(f"User id={user.id}\n")

    # ── Find test repos ──────────────────────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(Repo).where(
                Repo.user_id == user.id,
                Repo.name.like("test-repo%"),
            )
        )
        test_repos: list[Repo] = list(result.scalars().all())

    if not test_repos:
        print("No test-repo-* rows found for this user — nothing to clean up.")
        await engine.dispose()
        return

    print(f"Found {len(test_repos)} test repo(s) to delete:")
    for repo in test_repos:
        print(f"  • id={repo.id}  name={repo.name!r}  github_repo_id={repo.github_repo_id}")

    # Count commits that will be removed.
    test_repo_ids = [r.id for r in test_repos]
    async with session_factory() as session:
        result = await session.execute(
            select(Commit).where(Commit.repo_id.in_(test_repo_ids))
        )
        test_commits: list[Commit] = list(result.scalars().all())

    print(f"\nAssociated Commit rows to delete: {len(test_commits)}")

    if dry_run:
        print("\n[DRY RUN] No changes made — pass without --dry-run to execute.")
        await engine.dispose()
        return

    # ── /stats/repos BEFORE cleanup ──────────────────────────────────────────
    if not skip_api_check:
        print("\n-- /stats/repos BEFORE cleanup ----------------------------------------")
        repos_before = await fetch_stats_repos(user)
        for r in repos_before:
            print(f"  {r['repo_name']:<40}  commits={r['commit_count']}")
        if not repos_before:
            print("  (no active repos in last 30 days)")

    # ── Delete commits, then repos ───────────────────────────────────────────
    print("\n-- Deleting rows -------------------------------------------------------")
    async with session_factory() as session:
        # Delete commits first (FK constraint)
        del_commits = await session.execute(
            delete(Commit).where(Commit.repo_id.in_(test_repo_ids))
        )
        commits_deleted = del_commits.rowcount

        # Delete repos
        del_repos = await session.execute(
            delete(Repo).where(Repo.id.in_(test_repo_ids))
        )
        repos_deleted = del_repos.rowcount

        await session.commit()

    print(f"  Commits deleted: {commits_deleted}")
    print(f"  Repos deleted  : {repos_deleted}")

    # ── /stats/repos AFTER cleanup ───────────────────────────────────────────
    if not skip_api_check:
        print("\n-- /stats/repos AFTER cleanup -----------------------------------------")
        repos_after = await fetch_stats_repos(user)
        for r in repos_after:
            print(f"  {r['repo_name']:<40}  commits={r['commit_count']}")
        if not repos_after:
            print("  (no active repos in last 30 days)")

        # Confirm none of the deleted repos appear in the response.
        deleted_names = {r.name for r in test_repos}
        leaked = [r for r in repos_after if r["repo_name"] in deleted_names]
        if leaked:
            print(f"\n  WARNING: {len(leaked)} deleted repo(s) still appear in /stats/repos!")
            for r in leaked:
                print(f"    • {r['repo_name']}")
        else:
            print("\n  [PASS] No test-repo-* entries remain in /stats/repos.")

    print("\n" + "=" * 60)
    print(f"  Cleanup complete: {repos_deleted} repo(s), {commits_deleted} commit(s) removed.")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Delete test-repo-* Repo/Commit rows from the live dev database."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without making any DB changes.",
    )
    parser.add_argument(
        "--skip-api-check",
        action="store_true",
        help="Skip the before/after /stats/repos HTTP check (uses ASGITransport).",
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, skip_api_check=args.skip_api_check))
