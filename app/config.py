"""Centralised application settings loaded from environment / .env file.

Uses pydantic-settings so that every config value is validated at startup.
Add new env vars here — they'll be picked up automatically from .env or
the process environment.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration.

    Values are read (in priority order) from:
    1. Environment variables already set in the process
    2. A ``.env`` file in the project root
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # silently skip env vars we don't declare
    )

    # ── Database ─────────────────────────────────────────────────
    DATABASE_URL: str

    # ── GitHub OAuth ─────────────────────────────────────────────
    GITHUB_CLIENT_ID: str
    GITHUB_CLIENT_SECRET: str
    GITHUB_REDIRECT_URI: str = "http://localhost:8000/auth/github/callback"

    # ── Encryption ───────────────────────────────────────────────
    ENCRYPTION_KEY: str  # Fernet key — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # ── JWT ───────────────────────────────────────────────────────
    JWT_SECRET_KEY: str   # generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080  # 7 days

    # ── Redis (caching) ───────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Frontend ─────────────────────────────────────────────────
    FRONTEND_REDIRECT_URL: str = "http://localhost:5173"
    FRONTEND_URL: str = "http://localhost:5173" # comma-separated list of allowed origins


# Module-level singleton so the rest of the app can just
# ``from app.config import settings``.
settings = Settings()
