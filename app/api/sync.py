"""Sync routes — trigger GitHub data synchronisation for the current user.

Flow (POST /sync/github):
    1. Decrypt the user's stored GitHub access token.
    2. sync_repos()              — upsert all repos from GitHub.
    3. sync_commits()            — incrementally fetch new commits for every repo.
    4. compute_daily_snapshots() — recompute per-day commit aggregates.
    5. Return a JSON summary of what changed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.db import async_session
from app.dependencies import get_current_user
from app.models.user import User
from app.services.github_client import GitHubAuthError, GitHubAPIError, GitHubClient
from app.services.sync_service import compute_daily_snapshots, sync_commits, sync_repos
from app.utils.encryption import decrypt_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/github")
async def sync_github(user: User = Depends(get_current_user)) -> dict:
    """Run the full GitHub sync pipeline for the authenticated user.

    Steps (in order):
        1. ``sync_repos``              — upsert all repos from GitHub.
        2. ``sync_commits``            — incremental fetch of new commits per repo.
        3. ``compute_daily_snapshots`` — recompute per-day commit aggregates.

    Returns a JSON summary:
        repos_synced        – total repos upserted (owned + collaborated).
        new_commits_by_repo – mapping of repo name → new commits inserted.
        total_new_commits   – sum of all new commits across every repo.
        snapshot_rows_upserted – DailySnapshot rows created or updated.

    Raises:
        HTTP 401 – if the user has no stored access token.
        HTTP 502 – if GitHub returns an auth or API error.
        HTTP 500 – for any unexpected failure, with the step name logged.
    """
    if not user.github_access_token_encrypted:
        raise HTTPException(
            status_code=401,
            detail="No GitHub access token on file — please re-authenticate via /auth/github/login.",
        )

    access_token = decrypt_token(user.github_access_token_encrypted)
    github_client = GitHubClient(access_token=access_token)

    # ── Step 1: sync repos ───────────────────────────────────────────────────
    try:
        async with async_session() as session:
            repos = await sync_repos(session, user, github_client)
    except (GitHubAuthError, GitHubAPIError) as exc:
        logger.error(
            "sync_github: sync_repos failed for user_id=%s — %s",
            user.id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error during repo sync: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception(
            "sync_github: unexpected error in sync_repos for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during repo sync — see server logs for details.",
        ) from exc

    # ── Step 2: sync commits ─────────────────────────────────────────────────
    try:
        async with async_session() as session:
            new_commit_counts: dict[int, int] = await sync_commits(
                session, user, github_client, repos
            )
    except (GitHubAuthError, GitHubAPIError) as exc:
        logger.error(
            "sync_github: sync_commits failed for user_id=%s — %s",
            user.id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error during commit sync: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception(
            "sync_github: unexpected error in sync_commits for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during commit sync — see server logs for details.",
        ) from exc

    # ── Step 3: compute daily snapshots ──────────────────────────────────────
    try:
        async with async_session() as session:
            snapshot_rows_upserted: int = await compute_daily_snapshots(session, user)
    except Exception as exc:
        logger.exception(
            "sync_github: unexpected error in compute_daily_snapshots for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during daily snapshot computation — see server logs for details.",
        ) from exc

    # ── Build response summary ────────────────────────────────────────────────
    # Map repo.id → repo.name so the response is human-readable.
    repo_name_by_id: dict[int, str] = {r.id: r.name for r in repos}
    new_commits_by_repo: dict[str, int] = {
        repo_name_by_id[repo_id]: count
        for repo_id, count in new_commit_counts.items()
    }
    total_new_commits: int = sum(new_commit_counts.values())

    logger.info(
        "sync_github: completed for user_id=%s — repos=%d, new_commits=%d, snapshots_upserted=%d",
        user.id,
        len(repos),
        total_new_commits,
        snapshot_rows_upserted,
    )

    return {
        "status": "ok",
        "repos_synced": len(repos),
        "new_commits_by_repo": new_commits_by_repo,
        "total_new_commits": total_new_commits,
        "snapshot_rows_upserted": snapshot_rows_upserted,
    }
