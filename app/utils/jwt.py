"""JWT creation and validation helpers using PyJWT.

Generate a secret key with::

    python -c "import secrets; print(secrets.token_urlsafe(32))"
"""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings


def create_access_token(user_id: int, github_username: str) -> str:
    """Create a signed JWT containing the user's identity.

    Payload:
        sub            – stringified user ID (JWT convention)
        github_username – for convenient frontend display
        exp            – expiration timestamp
    """
    payload = {
        "sub": str(user_id),
        "github_username": github_username,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT.

    Raises:
        jwt.ExpiredSignatureError – if the token has expired.
        jwt.InvalidTokenError    – if the token is malformed or tampered with.
    """
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
