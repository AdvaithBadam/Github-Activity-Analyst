"""Async HTTP client for the GitHub REST API.

This module is **purely** an HTTP client — no database imports, no ORM models,
no upsert logic.  It fetches data from GitHub and returns raw dicts so that
higher-level service/sync layers can decide what to persist.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


# ── Exceptions ───────────────────────────────────────────────────


class GitHubAuthError(Exception):
    """Raised when GitHub returns 401 (bad token) or 403 (forbidden/revoked)."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub auth error ({status_code}): {body}")


class GitHubAPIError(Exception):
    """Raised for any non-2xx response that isn't an auth error."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub API error ({status_code}): {body}")


# ── Client ───────────────────────────────────────────────────────


class GitHubClient:
    """Lightweight async wrapper around the GitHub REST API.

    Usage::

        client = GitHubClient(access_token="ghp_xxxx")
        repos = await client.get_repos()
        commits = await client.get_commits("owner", "repo")
    """

    BASE_URL = "https://api.github.com"
    _RATE_LIMIT_WARNING_THRESHOLD = 100

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }

    # ── Public API ───────────────────────────────────────────────

    async def get_repos(self) -> list[dict]:
        """Return every repo owned by the authenticated user.

        Follows GitHub's ``Link`` header pagination automatically.
        """
        return await self._paginate("/user/repos")

    async def get_commits(
        self,
        owner: str,
        repo: str,
        since: datetime | None = None,
    ) -> list[dict]:
        """Return commits for a given repository.

        Parameters
        ----------
        owner:
            Repository owner (user or organisation login).
        repo:
            Repository name.
        since:
            If provided, only commits after this datetime are returned
            (passed as ISO 8601 to GitHub).
        """
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = since.isoformat()

        return await self._paginate(f"/repos/{owner}/{repo}/commits", params=params)

    # ── Internals ────────────────────────────────────────────────

    async def _paginate(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> list[dict]:
        """Fetch all pages for a paginated GitHub endpoint.

        Uses a single ``httpx.AsyncClient`` for the entire pagination run
        to reuse the underlying TCP connection.
        """
        results: list[dict] = []
        url: str | None = f"{self.BASE_URL}{path}"

        # Merge per_page into the caller-supplied params for the first request.
        merged_params: dict[str, str] = {"per_page": "100"}
        if params:
            merged_params.update(params)

        async with httpx.AsyncClient(headers=self._headers) as client:
            # First request uses the built URL + query params.
            response = await client.get(url, params=merged_params)  # type: ignore[arg-type]
            self._check_response(response)
            results.extend(response.json())

            # Subsequent requests follow the "next" Link header directly
            # (the URL already contains query params).
            while (url := self._next_link(response)) is not None:
                response = await client.get(url)
                self._check_response(response)
                results.extend(response.json())

        return results

    # ── Helpers ───────────────────────────────────────────────────

    def _check_response(self, response: httpx.Response) -> None:
        """Validate a GitHub response: check status and rate-limit headers."""
        # Rate-limit monitoring
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < self._RATE_LIMIT_WARNING_THRESHOLD:
            logger.warning(
                "GitHub rate limit running low: %s requests remaining",
                remaining,
            )

        # Auth errors
        if response.status_code in (401, 403):
            raise GitHubAuthError(response.status_code, response.text)

        # Any other non-2xx
        if not (200 <= response.status_code < 300):
            raise GitHubAPIError(response.status_code, response.text)

    @staticmethod
    def _next_link(response: httpx.Response) -> str | None:
        """Parse the ``Link`` header and return the ``rel="next"`` URL, if any."""
        link_header = response.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                # Format: <https://api.github.com/...?page=2>; rel="next"
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None
