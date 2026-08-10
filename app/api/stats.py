"""Activity statistics routes.

Routes:
    GET /stats/summary -- return four activity metrics for the current user:
        current_streak, longest_streak, weekly_velocity, active_repos.

Caching
-------
Results are cached in Redis under key ``stats_summary:{user_id}`` with a
300-second TTL (cache-aside pattern).  Cache failures are fail-open: a Redis
outage causes the route to compute stats from the DB normally rather than
returning a 500.
"""

from __future__ import annotations

import datetime
import json
import logging
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import distinct, func, select

from app.cache import get_redis_client
from app.db import async_session
from app.dependencies import get_current_user
from app.models.commit import Commit
from app.models.daily_snapshot import DailySnapshot
from app.models.repo import Repo
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["stats"])

# Cache key pattern and TTL
_CACHE_KEY = "stats_summary:{user_id}"
_CACHE_TTL_SECONDS = 300  # 5 minutes


# ── Streak helpers ────────────────────────────────────────────────


def _compute_current_streak(active_dates: set[date], today_utc: date) -> int:
    """Return the current streak length ending on *today_utc* or *yesterday_utc*.

    Rules
    -----
    - If today has a commit, count backwards from today until a gap.
    - If today has no commit (or it's zero/missing), check yesterday:
      - If yesterday has a commit, count backwards from yesterday.
      - Otherwise streak is 0.

    Parameters
    ----------
    active_dates:
        Set of ``date`` objects for which commit_count >= 1.
    today_utc:
        The UTC calendar date used as "now".
    """
    yesterday = today_utc - timedelta(days=1)

    # Determine the anchor: today if active, else yesterday if active, else 0.
    if today_utc in active_dates:
        anchor = today_utc
    elif yesterday in active_dates:
        anchor = yesterday
    else:
        return 0

    streak = 0
    cursor = anchor
    while cursor in active_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _compute_longest_streak(active_dates: set[date]) -> int:
    """Return the longest consecutive run of dates in *active_dates*.

    Parameters
    ----------
    active_dates:
        Set of ``date`` objects for which commit_count >= 1.
    """
    if not active_dates:
        return 0

    sorted_dates = sorted(active_dates)
    longest = 1
    current = 1

    for i in range(1, len(sorted_dates)):
        if sorted_dates[i] - sorted_dates[i - 1] == timedelta(days=1):
            current += 1
            if current > longest:
                longest = current
        else:
            current = 1

    return longest


# ── Route ────────────────────────────────────────────────────────


@router.get("/summary")
async def get_stats_summary(user: User = Depends(get_current_user)) -> dict:
    """Return four activity-summary metrics for the authenticated user.

    Metrics
    -------
    current_streak:
        Consecutive days ending today or yesterday (UTC) with commit_count >= 1.
        If today has no snapshot yet, yesterday is checked — the streak is only
        broken when a full UTC day passes with zero commits.
    longest_streak:
        Longest consecutive run of UTC calendar days with commit_count >= 1
        across the user's entire DailySnapshot history.
    weekly_velocity:
        Raw sum of commit_count across DailySnapshot rows in the last 7 days
        (rolling window from now, inclusive of today).
    active_repos:
        Count of distinct repos with at least one Commit where committed_at
        falls within the last 14 days (rolling window from now, UTC).
        Computed from the Commit table joined to Repo — does not use
        DailySnapshot (which has no per-repo data).
    computed_at_utc:
        The exact UTC timestamp used as "now" for all rolling-window
        calculations (ISO 8601).

    A user who has never synced (no DailySnapshot rows) gets all zeros
    rather than a 404 or 500.

    Raises
    ------
    HTTP 500 – on any unexpected database failure, with the failing metric
               and user id logged.
    """
    now_utc: datetime.datetime = datetime.datetime.now(timezone.utc)
    today_utc: date = now_utc.date()
    seven_days_ago: datetime.datetime = now_utc - timedelta(days=6)
    fourteen_days_ago: datetime.datetime = now_utc - timedelta(days=14)

    cache_key = _CACHE_KEY.format(user_id=user.id)

    # ── Cache read (fail-open) ───────────────────────────────────────────────
    try:
        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = json.loads(cached)
            payload["cache_hit"] = True
            logger.info(
                "get_stats_summary: cache HIT for user_id=%s",
                user.id,
            )
            return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_summary: Redis read failed for user_id=%s — falling through to DB: %s",
            user.id,
            exc,
        )

    # ── 1. Fetch all active-commit dates (used for both streak metrics) ──────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(DailySnapshot.date)
                .where(
                    DailySnapshot.user_id == user.id,
                    DailySnapshot.commit_count >= 1,
                )
                .order_by(DailySnapshot.date)
            )
            active_dates: set[date] = set(result.scalars().all())
    except Exception as exc:
        logger.exception(
            "get_stats_summary: failed fetching active snapshot dates for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing streak metrics — see server logs.",
        ) from exc

    # ── 2. Current streak & longest streak (pure Python) ────────────────────
    current_streak: int = _compute_current_streak(active_dates, today_utc)
    longest_streak: int = _compute_longest_streak(active_dates)

    # ── 3. Weekly velocity (last 7 days) ─────────────────────────────────────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(DailySnapshot.commit_count), 0))
                .where(
                    DailySnapshot.user_id == user.id,
                    DailySnapshot.date >= seven_days_ago.date(),
                )
            )
            weekly_velocity: int = result.scalar_one()
    except Exception as exc:
        logger.exception(
            "get_stats_summary: failed computing weekly_velocity for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing weekly velocity — see server logs.",
        ) from exc

    # ── 4. Active repos (last 14 days, from Commit table) ────────────────────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(func.count(distinct(Commit.repo_id)))
                .join(Repo, Commit.repo_id == Repo.id)
                .where(
                    Repo.user_id == user.id,
                    Commit.committed_at >= fourteen_days_ago,
                )
            )
            active_repos: int = result.scalar_one()
    except Exception as exc:
        logger.exception(
            "get_stats_summary: failed computing active_repos for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing active repos — see server logs.",
        ) from exc

    logger.info(
        "get_stats_summary: cache MISS for user_id=%s -- current_streak=%d, "
        "longest_streak=%d, weekly_velocity=%d, active_repos=%d",
        user.id,
        current_streak,
        longest_streak,
        weekly_velocity,
        active_repos,
    )

    result_payload = {
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "weekly_velocity": weekly_velocity,
        "active_repos": active_repos,
        "computed_at_utc": now_utc.isoformat(),
        "cache_hit": False,
    }

    # ── Cache write (fail-open) ──────────────────────────────────────────────
    try:
        redis = get_redis_client()
        await redis.set(cache_key, json.dumps(result_payload), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_summary: Redis write failed for user_id=%s — result NOT cached: %s",
            user.id,
            exc,
        )

    return result_payload
