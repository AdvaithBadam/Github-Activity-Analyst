"""FastAPI application entry-point.

Run with:
    uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app.api.auth import router as auth_router

app = FastAPI(
    title="GitHub Activity Analyst",
    description="Track and analyse GitHub activity across users and repositories.",
    version="0.1.0",
)

# ── Routers ──────────────────────────────────────────────────────
app.include_router(auth_router)
