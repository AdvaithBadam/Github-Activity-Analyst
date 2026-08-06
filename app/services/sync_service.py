"""Repo sync service — fetches repos from GitHub and upserts into the database.

This module bridges the GitHubClient (HTTP-only) and the ORM models.
It does NOT sync commits — that is handled separately.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        is_owner = raw["owner"]["login"].lower() == user.github_username.lower()
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
                is_owner=is_owner,
                github_created_at=github_created_at,
            )
            session.add(repo)

        synced.append(repo)

    await session.commit()
    return synced
