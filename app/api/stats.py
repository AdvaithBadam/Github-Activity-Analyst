"""Activity statistics routes.

Routes:
    GET /stats/summary          -- four activity metrics (streak, velocity, repos).
    GET /stats/heatmap          -- daily commit counts for the last 365 days.
    GET /stats/repos            -- per-repo commit activity for the last 30 days.
    GET /stats/activity-pattern -- commit histograms by hour-of-day and day-of-week.

Caching
-------
All routes use a cache-aside pattern keyed by user_id with a 300-second TTL.
Cache failures are fail-open: a Redis outage causes each route to compute from
the DB normally rather than returning a 500.
"""

from __future__ import annotations

import datetime
import json
import logging
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import distinct, extract, func, label, select

from app.cache import get_redis_client
from app.db import async_session
from app.dependencies import get_current_user
from app.models.commit import Commit
from app.models.daily_snapshot import DailySnapshot
from app.models.repo import Repo
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["stats"])

# Cache key patterns and shared TTL — must stay in sync with cache.py.
_CACHE_KEY          = "stats_summary:{user_id}"
_HEATMAP_CACHE_KEY  = "stats_heatmap:{user_id}"
_REPOS_CACHE_KEY    = "stats_repos:{user_id}"
_PATTERN_CACHE_KEY  = "stats_activity_pattern:{user_id}"
_CACHE_TTL_SECONDS  = 300  # 5 minutes


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


# ── GET /stats/heatmap ───────────────────────────────────────────


@router.get("/heatmap")
async def get_stats_heatmap(user: User = Depends(get_current_user)) -> dict:
    """Return daily commit counts for the last 365 UTC calendar days.

    Response
    --------
    days:
        List of 365 objects — one per calendar day, oldest first.
        Every day in the window is present; days with no DailySnapshot row
        appear with commit_count: 0 so the frontend can draw a full grid.
    computed_at_utc:
        UTC timestamp used as "today" for the window calculation.

    A user with no snapshot history gets all-zero days rather than a 404/500.
    """
    now_utc: datetime.datetime = datetime.datetime.now(timezone.utc)
    today_utc: date = now_utc.date()
    window_start: date = today_utc - timedelta(days=364)  # inclusive → 365 days total

    cache_key = _HEATMAP_CACHE_KEY.format(user_id=user.id)

    # ── Cache read (fail-open) ───────────────────────────────────────────────
    try:
        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = json.loads(cached)
            payload["cache_hit"] = True
            logger.info("get_stats_heatmap: cache HIT for user_id=%s", user.id)
            return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_heatmap: Redis read failed for user_id=%s — falling through to DB: %s",
            user.id,
            exc,
        )

    # ── Fetch DailySnapshot rows in the 365-day window ───────────────────────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(DailySnapshot.date, DailySnapshot.commit_count)
                .where(
                    DailySnapshot.user_id == user.id,
                    DailySnapshot.date >= window_start,
                )
            )
            rows = result.all()
    except Exception as exc:
        logger.exception(
            "get_stats_heatmap: failed fetching DailySnapshot rows for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing heatmap — see server logs.",
        ) from exc

    # Build a lookup {date: commit_count} from DB rows, then fill every day.
    counts_by_date: dict[date, int] = {row.date: row.commit_count for row in rows}
    days = [
        {
            "date": (window_start + timedelta(days=i)).isoformat(),
            "commit_count": counts_by_date.get(window_start + timedelta(days=i), 0),
        }
        for i in range(365)
    ]

    logger.info(
        "get_stats_heatmap: cache MISS for user_id=%s — %d days, %d with commits",
        user.id,
        len(days),
        sum(1 for d in days if d["commit_count"] > 0),
    )

    result_payload = {
        "days": days,
        "computed_at_utc": now_utc.isoformat(),
        "cache_hit": False,
    }

    # ── Cache write (fail-open) ──────────────────────────────────────────────
    try:
        redis = get_redis_client()
        await redis.set(cache_key, json.dumps(result_payload), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_heatmap: Redis write failed for user_id=%s — result NOT cached: %s",
            user.id,
            exc,
        )

    return result_payload


# ── GET /stats/repos ─────────────────────────────────────────────


@router.get("/repos")
async def get_stats_repos(user: User = Depends(get_current_user)) -> dict:
    """Return per-repo commit activity for the last 30 days.

    Response
    --------
    repos:
        List sorted by commit_count descending.  Repos with zero commits in
        the 30-day window are excluded entirely — this is an active-activity
        view, not a full repo listing.
    computed_at_utc:
        UTC timestamp used as "now" for the rolling window.

    A user with no commits returns an empty repos list rather than a 404/500.
    """
    now_utc: datetime.datetime = datetime.datetime.now(timezone.utc)
    thirty_days_ago: datetime.datetime = now_utc - timedelta(days=30)

    cache_key = _REPOS_CACHE_KEY.format(user_id=user.id)

    # ── Cache read (fail-open) ───────────────────────────────────────────────
    try:
        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = json.loads(cached)
            payload["cache_hit"] = True
            logger.info("get_stats_repos: cache HIT for user_id=%s", user.id)
            return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_repos: Redis read failed for user_id=%s — falling through to DB: %s",
            user.id,
            exc,
        )

    # ── Query: commits per repo in the last 30 days ──────────────────────────
    try:
        async with async_session() as session:
            result = await session.execute(
                select(
                    Repo.name,
                    func.count(Commit.id).label("commit_count"),
                    func.max(Commit.committed_at).label("last_commit_at"),
                )
                .join(Repo, Commit.repo_id == Repo.id)
                .where(
                    Repo.user_id == user.id,
                    Commit.committed_at >= thirty_days_ago,
                )
                .group_by(Repo.name)
                .order_by(func.count(Commit.id).desc())
            )
            rows = result.all()
    except Exception as exc:
        logger.exception(
            "get_stats_repos: failed fetching per-repo activity for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing repo activity — see server logs.",
        ) from exc

    repos = [
        {
            "repo_name": row.name,
            "commit_count": row.commit_count,
            "last_commit_at": row.last_commit_at.isoformat() if row.last_commit_at else None,
        }
        for row in rows
    ]

    logger.info(
        "get_stats_repos: cache MISS for user_id=%s — %d active repos",
        user.id,
        len(repos),
    )

    result_payload = {
        "repos": repos,
        "computed_at_utc": now_utc.isoformat(),
        "cache_hit": False,
    }

    # ── Cache write (fail-open) ──────────────────────────────────────────────
    try:
        redis = get_redis_client()
        await redis.set(cache_key, json.dumps(result_payload), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_repos: Redis write failed for user_id=%s — result NOT cached: %s",
            user.id,
            exc,
        )

    return result_payload


# ── GET /stats/activity-pattern ──────────────────────────────────

_DOW_NAMES: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)


@router.get("/activity-pattern")
async def get_stats_activity_pattern(user: User = Depends(get_current_user)) -> dict:
    """Return all-time commit histograms by hour-of-day (UTC) and day-of-week.

    Response
    --------
    by_hour_utc:
        24 objects, one per UTC hour (0–23), always complete even if
        commit_count is 0 for some hours.
    by_day_of_week:
        7 objects, Monday–Sunday, always complete even if commit_count is 0.
    computed_at_utc:
        UTC timestamp at time of computation.

    Uses all-time Commit history for the user (no rolling window) so the
    pattern is stable and not skewed by recent quiet periods.
    A user with no commits gets all-zero buckets rather than a 404/500.
    """
    now_utc: datetime.datetime = datetime.datetime.now(timezone.utc)

    cache_key = _PATTERN_CACHE_KEY.format(user_id=user.id)

    # ── Cache read (fail-open) ───────────────────────────────────────────────
    try:
        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached is not None:
            payload = json.loads(cached)
            payload["cache_hit"] = True
            logger.info("get_stats_activity_pattern: cache HIT for user_id=%s", user.id)
            return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_activity_pattern: Redis read failed for user_id=%s "
            "— falling through to DB: %s",
            user.id,
            exc,
        )

    # ── Query: commits by UTC hour ───────────────────────────────────────────
    try:
        async with async_session() as session:
            _committed_at_utc = func.timezone("UTC", Commit.committed_at)
            _hour_expr = extract("hour", _committed_at_utc)
            result = await session.execute(
                select(
                    _hour_expr.label("hour"),
                    func.count(Commit.id).label("commit_count"),
                )
                .join(Repo, Commit.repo_id == Repo.id)
                .where(Repo.user_id == user.id)
                .group_by(_hour_expr)
            )
            hour_rows = result.all()
    except Exception as exc:
        logger.exception(
            "get_stats_activity_pattern: failed fetching by-hour counts for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing activity pattern (by hour) — see server logs.",
        ) from exc

    # ── Query: commits by ISO day-of-week (1=Monday … 7=Sunday) ─────────────
    try:
        async with async_session() as session:
            _committed_at_utc = func.timezone("UTC", Commit.committed_at)
            _isodow_expr = extract("isodow", _committed_at_utc)
            result = await session.execute(
                select(
                    _isodow_expr.label("isodow"),
                    func.count(Commit.id).label("commit_count"),
                )
                .join(Repo, Commit.repo_id == Repo.id)
                .where(Repo.user_id == user.id)
                .group_by(_isodow_expr)
            )
            dow_rows = result.all()
    except Exception as exc:
        logger.exception(
            "get_stats_activity_pattern: failed fetching by-dow counts for user_id=%s",
            user.id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unexpected error computing activity pattern (by day-of-week) — see server logs.",
        ) from exc

    # Build complete 24-hour axis (fill missing hours with 0).
    hour_counts: dict[int, int] = {int(r.hour): r.commit_count for r in hour_rows}
    by_hour_utc = [
        {"hour": h, "commit_count": hour_counts.get(h, 0)}
        for h in range(24)
    ]

    # Build complete 7-day axis (isodow 1=Mon … 7=Sun → index 0–6).
    dow_counts: dict[int, int] = {int(r.isodow): r.commit_count for r in dow_rows}
    by_day_of_week = [
        {"day": _DOW_NAMES[d], "commit_count": dow_counts.get(d + 1, 0)}
        for d in range(7)
    ]

    logger.info(
        "get_stats_activity_pattern: cache MISS for user_id=%s — "
        "total hour buckets with data=%d, total dow buckets with data=%d",
        user.id,
        sum(1 for b in by_hour_utc if b["commit_count"] > 0),
        sum(1 for b in by_day_of_week if b["commit_count"] > 0),
    )

    result_payload = {
        "by_hour_utc": by_hour_utc,
        "by_day_of_week": by_day_of_week,
        "computed_at_utc": now_utc.isoformat(),
        "cache_hit": False,
    }

    # ── Cache write (fail-open) ──────────────────────────────────────────────
    try:
        redis = get_redis_client()
        await redis.set(cache_key, json.dumps(result_payload), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_stats_activity_pattern: Redis write failed for user_id=%s "
            "— result NOT cached: %s",
            user.id,
            exc,
        )

    return result_payload
