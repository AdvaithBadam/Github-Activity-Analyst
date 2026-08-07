"""Sync services — fetch data from GitHub and upsert into the database.

This module bridges the GitHubClient (HTTP-only) and the ORM models.

Functions:
    sync_repos             – upsert all repos for a user
    sync_commits           – upsert commits for a list of repos (incremental)
    compute_daily_snapshots – recompute daily commit snapshots for a user
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commit import Commit
from app.models.daily_snapshot import DailySnapshot
from app.models.repo import Repo
from app.models.user import User
from app.services.github_client import GitHubClient


async def sync_repos(
    session: AsyncSession,
    user: User,
    github_client: GitHubClient,
) -> list[Repo]:
    """Fetch all repos from GitHub for *user* and upsert them into the database.

    Parameters
    ----------
    session:
        An active async SQLAlchemy session (caller manages the lifecycle).
    user:
        The authenticated ``User`` ORM object whose repos we're syncing.
    github_client:
        A ``GitHubClient`` initialised with the user's access token.

    Returns
    -------
    list[Repo]
        All Repo ORM objects that were created or updated, usable for
        subsequent commit sync without re-querying.
    """
    raw_repos = await github_client.get_repos()

    synced: list[Repo] = []

    for raw in raw_repos:
        github_repo_id: int = raw["id"]
        repo_name: str = raw["name"]
        owner_login: str = raw["owner"]["login"]
        is_owner = owner_login.lower() == user.github_username.lower()
        github_created_at = datetime.fromisoformat(
            raw["created_at"].replace("Z", "+00:00")
        )

        # Look up existing row by the stable GitHub numeric ID
        result = await session.execute(
            select(Repo).where(
                Repo.user_id == user.id,
                Repo.github_repo_id == github_repo_id,
            )
        )
        repo = result.scalar_one_or_none()

        if repo is not None:
            # Existing repo — update mutable fields if changed
            if repo.name != repo_name:
                repo.name = repo_name
            if repo.owner_login != owner_login:
                repo.owner_login = owner_login
            if repo.is_owner != is_owner:
                repo.is_owner = is_owner
            if repo.github_created_at != github_created_at:
                repo.github_created_at = github_created_at
        else:
            # New repo
            repo = Repo(
                user_id=user.id,
                github_repo_id=github_repo_id,
                name=repo_name,
                owner_login=owner_login,
                is_owner=is_owner,
                github_created_at=github_created_at,
            )
            session.add(repo)

        synced.append(repo)

    await session.commit()
    return synced


# ── Commit sync ──────────────────────────────────────────────────


async def sync_commits(
    session: AsyncSession,
    user: User,
    github_client: GitHubClient,
    repos: list[Repo],
) -> dict[int, int]:
    """Fetch commits from GitHub for each repo and insert new ones.

    Parameters
    ----------
    session:
        An active async SQLAlchemy session.
    user:
        The authenticated ``User`` whose repos we're syncing.
    github_client:
        A ``GitHubClient`` initialised with the user's access token.
    repos:
        The list of ``Repo`` ORM objects to sync commits for (typically
        the return value from ``sync_repos()``).

    Returns
    -------
    dict[int, int]
        Mapping of ``repo.id`` → count of **new** commits inserted.

    Notes
    -----
    Commits are committed to the database in a single flush at the end,
    not per-repo.  This is more efficient (one round-trip) but means a
    failure partway through rolls back *all* repos' commits.  For this
    use-case that's acceptable — the next sync will simply re-fetch and
    re-insert.  If you need per-repo atomicity, wrap each repo's block
    in its own ``async with session.begin_nested():`` savepoint.
    """
    # Default cutoff for first-time sync: 12 months of history
    default_since = datetime.now(timezone.utc) - timedelta(days=365)

    new_commit_counts: dict[int, int] = {}

    for repo in repos:
        owner = repo.owner_login or user.github_username
        repo_name = repo.name

        # ── Determine `since` cutoff ─────────────────────────────
        result = await session.execute(
            select(func.max(Commit.committed_at)).where(
                Commit.repo_id == repo.id
            )
        )
        latest_committed_at = result.scalar_one_or_none()
        since_cutoff = latest_committed_at if latest_committed_at is not None else default_since

        # ── Fetch commits from GitHub ────────────────────────────
        raw_commits = await github_client.get_commits(owner, repo_name, since=since_cutoff)

        # ── Upsert commits ───────────────────────────────────────
        new_count = 0

        # Batch-fetch existing SHAs for this repo to avoid N+1 queries
        existing_result = await session.execute(
            select(Commit.sha).where(Commit.repo_id == repo.id)
        )
        existing_shas: set[str] = set(existing_result.scalars().all())

        for raw in raw_commits:
            sha: str = raw["sha"]

            if sha in existing_shas:
                continue

            committed_at = datetime.fromisoformat(
                raw["commit"]["author"]["date"].replace("Z", "+00:00")
            )
            commit = Commit(
                repo_id=repo.id,
                sha=sha,
                message=raw["commit"]["message"],
                committed_at=committed_at,
            )
            session.add(commit)
            existing_shas.add(sha)  # prevent duplicates within the same batch
            new_count += 1

        new_commit_counts[repo.id] = new_count

    await session.commit()
    return new_commit_counts


# ── Daily Snapshot sync ──────────────────────────────────────────


async def compute_daily_snapshots(
    session: AsyncSession,
    user: User,
) -> int:
    """Recomputes DailySnapshot rows for a user from scratch based on Commit rows.

    Parameters
    ----------
    session:
        An active async SQLAlchemy session (caller manages the lifecycle).
    user:
        The authenticated ``User`` ORM object whose daily snapshots we're computing.

    Returns
    -------
    int
        The total number of DailySnapshot rows that were either created or updated.
    """
    stmt = (
        select(
            func.date(func.timezone("UTC", Commit.committed_at)).label("snapshot_date"),
            func.count(Commit.id).label("commit_count"),
        )
        .join(Repo, Commit.repo_id == Repo.id)
        .where(Repo.user_id == user.id)
        .group_by(func.date(func.timezone("UTC", Commit.committed_at)))
    )
    result = await session.execute(stmt)
    rows = result.all()

    modified_count = 0

    for snapshot_date, commit_count in rows:
        existing_result = await session.execute(
            select(DailySnapshot).where(
                DailySnapshot.user_id == user.id,
                DailySnapshot.date == snapshot_date,
            )
        )
        snapshot = existing_result.scalar_one_or_none()

        if snapshot is not None:
            if snapshot.commit_count != commit_count:
                snapshot.commit_count = commit_count
                modified_count += 1
        else:
            snapshot = DailySnapshot(
                user_id=user.id,
                date=snapshot_date,
                commit_count=commit_count,
            )
            session.add(snapshot)
            modified_count += 1

    await session.commit()
    return modified_count

