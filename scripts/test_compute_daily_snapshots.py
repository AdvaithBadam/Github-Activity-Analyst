"""Standalone smoke-test for compute_daily_snapshots().

Proves the daily snapshot computation is:
  1. Correct — snapshot counts match a raw DB aggregation for a sampled date.
  2. Idempotent — running twice produces identical rows.

Prerequisites:
    1. Apply all migrations:  alembic upgrade head
    2. Run test_sync_repos.py to create a User and Repo rows.
    3. Run test_sync_commits.py to populate Commit rows.

Run::

    python scripts/test_compute_daily_snapshots.py
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
from app.models.daily_snapshot import DailySnapshot
from app.models.repo import Repo
from app.models.user import User
from app.services.sync_service import compute_daily_snapshots

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)


async def fetch_snapshots(
    session_factory: async_sessionmaker,
    user_id: int,
) -> list[DailySnapshot]:
    """Return all DailySnapshot rows for a user, ordered by date ascending."""
    async with session_factory() as session:
        result = await session.execute(
            select(DailySnapshot)
            .where(DailySnapshot.user_id == user_id)
            .order_by(DailySnapshot.date)
        )
        return list(result.scalars().all())


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # ── Load existing User ───────────────────────────────────────────────────
    async with session_factory() as session:
        result = await session.execute(select(User))
        user = result.scalars().first()

    if user is None:
        print("ERROR: No User row found in the database.")
        print("       Run test_sync_repos.py first to create a User and sync repos,")
        print("       then run test_sync_commits.py to populate Commit rows.")
        sys.exit(1)

    print(f"Found User (id={user.id}, github_username={user.github_username!r})\n")

    # Quick sanity check: ensure commits exist for this user
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(Commit.id))
            .join(Repo, Commit.repo_id == Repo.id)
            .where(Repo.user_id == user.id)
        )
        total_commits = result.scalar_one()

    if total_commits == 0:
        print("ERROR: No Commit rows found for this user.")
        print("       Run test_sync_commits.py first to populate commits.")
        sys.exit(1)

    print(f"Total commits in DB for this user: {total_commits}\n")

    # ── Run 1: initial compute ───────────────────────────────────────────────
    print("-- Run 1: compute_daily_snapshots --------------------------------")
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        user_attached = result.scalar_one()
        modified_count_1 = await compute_daily_snapshots(session, user_attached)

    snapshots_1 = await fetch_snapshots(session_factory, user.id)
    print(f"  Rows created/updated: {modified_count_1}")
    print(f"  Total DailySnapshot rows: {len(snapshots_1)}")
    if snapshots_1:
        print(f"  Date range: {snapshots_1[0].date} -> {snapshots_1[-1].date}")
    print()

    # ── Run 2: idempotency check ─────────────────────────────────────────────
    print("-- Run 2: compute_daily_snapshots (idempotency check) ----------")
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.id == user.id))
        user_attached = result.scalar_one()
        modified_count_2 = await compute_daily_snapshots(session, user_attached)

    snapshots_2 = await fetch_snapshots(session_factory, user.id)
    print(f"  Rows created/updated: {modified_count_2}")
    print(f"  Total DailySnapshot rows: {len(snapshots_2)}")
    print()

    # ── Assertion 1: identical row count ────────────────────────────────────
    assert len(snapshots_1) == len(snapshots_2), (
        f"FAIL: row count differs between runs: {len(snapshots_1)} vs {len(snapshots_2)}"
    )

    # ── Assertion 2: identical (date, commit_count) values ───────────────────
    pairs_1 = [(s.date, s.commit_count) for s in snapshots_1]
    pairs_2 = [(s.date, s.commit_count) for s in snapshots_2]
    assert pairs_1 == pairs_2, (
        "FAIL: (date, commit_count) pairs differ between runs.\n"
        f"  Run 1: {pairs_1}\n"
        f"  Run 2: {pairs_2}"
    )

    print("[PASS] Idempotency check PASSED -- row count and (date, commit_count) pairs are identical across both runs.")
    print()

    # ── Cross-check: raw DB aggregation for a sampled date ───────────────────
    # Pick a middle snapshot (not first, not last) to avoid edge-case dates.
    if len(snapshots_1) < 3:
        print("NOTE: Fewer than 3 snapshot rows available; sampling the middle row for cross-check.")

    mid_idx = len(snapshots_1) // 2
    sampled = snapshots_1[mid_idx]
    sampled_date = sampled.date
    snapshot_count = sampled.commit_count

    print(f"-- Cross-check for sampled date: {sampled_date} -----------------")

    # Independently compute commit count for that date via a raw aggregation.
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(Commit.id))
            .join(Repo, Commit.repo_id == Repo.id)
            .where(
                Repo.user_id == user.id,
                func.date(func.timezone("UTC", Commit.committed_at)) == sampled_date,
            )
        )
        raw_count = result.scalar_one()

    print(f"  Snapshot commit_count : {snapshot_count}")
    print(f"  Raw DB count          : {raw_count}")

    assert snapshot_count == raw_count, (
        f"FAIL: snapshot says {snapshot_count} commits on {sampled_date}, "
        f"but raw query found {raw_count}."
    )

    print(f"[PASS] Cross-check PASSED -- both methods agree on {raw_count} commit(s) for {sampled_date}.")
    print()
    print("=" * 62)
    print("  All checks passed.")
    print("=" * 62)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
