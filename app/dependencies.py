"""FastAPI dependencies for request-scoped concerns (auth, DB sessions, etc.)."""

import jwt
from fastapi import Cookie, HTTPException
from sqlalchemy import select

from app.db import async_session
from app.models.user import User
from app.utils.jwt import decode_access_token


async def get_current_user(access_token: str | None = Cookie(default=None)) -> User:
    """Resolve the current authenticated user from the JWT session cookie.

    Reads the ``access_token`` httponly cookie, decodes the JWT, and loads
    the corresponding ``User`` from the database.

    Raises:
        HTTPException(401) – cookie missing, token invalid/expired, or user
                             no longer exists in the database.
    """
    if access_token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = decode_access_token(access_token)
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = int(payload["sub"])

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=401, detail="User not found")

    return user
