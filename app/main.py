"""FastAPI application entry-point.

Run with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.stats import router as stats_router
from app.api.sync import router as sync_router
from app.config import settings

app = FastAPI(
    title="GitHub Activity Analyst",
    description="Track and analyse GitHub activity across users and repositories.",
    version="0.1.0",
)

# ── CORS ─────────────────────────────────────────────────────────
# Allow the Vite dev server to send credentialed requests (cookies)
# cross-origin.  Only the explicit frontend origin is whitelisted —
# wildcard ("*") is incompatible with allow_credentials=True.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(sync_router)
app.include_router(stats_router)
