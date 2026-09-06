"""GitHub OAuth routes — handshake + user upsert + JWT issuance.

Flow:
    1. GET /auth/github/login   → redirect user to GitHub authorize page
    2. GET /auth/github/callback → exchange code for token, fetch profile,
       upsert User row, issue JWT session cookie, return confirmation JSON
"""

import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.models.user import User
from app.utils.encryption import encrypt_token
from app.utils.jwt import create_access_token
from app.dependencies import get_current_user

# TODO: replace with your actual existing Redis client import — e.g.:
# from app.services.cache import redis_client
from app.cache import get_redis_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/github", tags=["auth"])

# ── Constants ────────────────────────────────────────────────────
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes — state must be used within this window


@router.get("/login")
async def github_login() -> RedirectResponse:
    """Kick off the OAuth flow by redirecting to GitHub's authorize page."""
    state = secrets.token_urlsafe(32)

    # Store state server-side (Redis) instead of in a cookie.
    # This avoids cross-site cookie policy issues entirely, since no
    # cookie needs to survive the redirect to github.com and back.
    try:
        await get_redis_client().set(
            f"oauth_state:{state}", "1", ex=OAUTH_STATE_TTL_SECONDS
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize OAuth flow — server storage unavailable.",
        ) from exc

    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
        "scope": "public_repo,read:user",
        "state": state,
    }
    return RedirectResponse(url=f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
async def github_callback(
    code: str = Query(..., description="Authorization code from GitHub"),
    state: str = Query(..., description="OAuth state for CSRF verification"),
):
    """Handle the OAuth callback from GitHub.

    1. Validate ``state`` exists in Redis (CSRF check) — consume it (delete)
       so it can't be replayed.
    2. Exchange the ``code`` for an access token.
    3. Use that token to fetch the authenticated user's profile.
    4. Upsert the User row in the database with the encrypted token.
    5. Issue a JWT session cookie and redirect to the frontend.
    """
        # ── Step 0: CSRF validation via Redis, not a cookie ──────────
    state_key = f"oauth_state:{state}"

    try:
        state_exists = await get_redis_client().get(state_key)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Failed to validate OAuth state — server storage unavailable.",
        ) from exc

    if not state_exists:
        raise HTTPException(
            status_code=400,
            detail="Invalid or missing OAuth state — possible CSRF attempt",
        )

    # Consume immediately — one-time use, prevents replay
    try:
        await get_redis_client().delete(state_key)
    except Exception as exc:
        logger.warning("Failed to delete consumed oauth_state key %s: %s", state_key, exc)

    async with httpx.AsyncClient() as client:
        # ── Step 1: exchange code → access_token ─────────────────
        token_response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )

        token_data = token_response.json()

        if "access_token" not in token_data:
            raise HTTPException(
                status_code=400,
                detail=f"GitHub token exchange failed: {token_data.get('error_description', token_data)}",
            )

        access_token: str = token_data["access_token"]

        # ── Step 2: fetch GitHub user profile ────────────────────
        user_response = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if user_response.status_code != 200:
            raise HTTPException(
                status_code=user_response.status_code,
                detail="Failed to fetch GitHub user profile.",
            )

        github_user = user_response.json()

    # ── Step 3: upsert User row ──────────────────────────────────
    github_username: str = github_user["login"]
    github_created_at = datetime.fromisoformat(
        github_user["created_at"].replace("Z", "+00:00")
    )
    encrypted_token = encrypt_token(access_token)

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.github_username == github_username)
        )
        user = result.scalar_one_or_none()

        if user is not None:
            user.github_access_token_encrypted = encrypted_token
            if user.github_created_at != github_created_at:
                user.github_created_at = github_created_at
        else:
            user = User(
                github_username=github_username,
                github_created_at=github_created_at,
                github_access_token_encrypted=encrypted_token,
            )
            session.add(user)

        await session.commit()

    # ── Step 4: issue JWT & respond ───────────────────────────────
    jwt_token = create_access_token(user.id, github_username)

    response = RedirectResponse(url=settings.FRONTEND_URL, status_code=302)
    response.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
    )
    return response


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    """Return the current authenticated user's public profile."""
    return {
        "id": user.id,
        "github_username": user.github_username,
        "github_created_at": user.github_created_at.isoformat(),
    }