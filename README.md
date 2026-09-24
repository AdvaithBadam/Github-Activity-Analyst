# GitHub Activity Analyst

Track and visualise your GitHub commit streaks, weekly velocity, and active repositories in real time.

**Live demo:** [github-activity-analyst.vercel.app](https://github-activity-analyst.vercel.app) (frontend) ·
Free-tier hosting: the first load after idle can take up to a minute.
[github-activity-analyst.onrender.com](https://github-activity-analyst.onrender.com) (backend)

<!-- screenshot -->

---

## Features

- **GitHub OAuth login** — one-click sign-in with your GitHub account.
- **Full sync pipeline** — repos (owned + collaborated), commits (incremental), and daily snapshots in a single request.
- **Streak tracking** — current streak and longest streak computed from UTC calendar days.
- **Weekly velocity** — rolling 7-day commit count.
- **Active repos** — distinct repos with commits in the last 14 days.
- **365-day contribution heatmap** — a CSS grid of daily commit counts, zero-filled for days with no activity.
- **Top repos** — per-repo commit breakdown for the last 30 days, sorted by commit count.
- **Activity patterns** — all-time commit histograms by hour of day (UTC) and day of week.
- **Non-owned repos** — repos the user doesn't own are tracked with `is_owner` and `owner_login` fields and surfaced with extra context in the stats.
- **Redis caching** — stats endpoints use a cache-aside pattern with a 300-second TTL; cache failures are fail-open.

---

## Architecture

```
┌──────────────────┐   1. load static build   ┌───────────────────────┐
│                  │─────────────────────────▶│ Vercel                │
│     Browser      │                          │ Vite + vanilla JS     │
│                  │◀─────────────────────────│ + Chart.js (static)   │
└────────┬─────────┘                          └───────────────────────┘
         │
         │  2. API calls (Authorization: Bearer <JWT>)
         ▼
┌─────────────────────────────────────────────┐
│  FastAPI on Render (Docker)                 │
│  uvicorn · Alembic · JWT auth               │
└──────┬──────────────┬───────────────┬───────┘
       │              │               │
       ▼              ▼               ▼
┌─────────────┐ ┌───────────┐ ┌─────────────────┐
│ Neon        │ │ Upstash   │ │ GitHub REST     │
│ (Postgres)  │ │ (Redis)   │ │ API             │
└─────────────┘ └───────────┘ └─────────────────┘
```

The frontend is a static Vite build deployed on Vercel. All API calls go to the FastAPI backend on Render, which talks to Neon (Postgres via asyncpg), Upstash (Redis via `redis.asyncio`), and the GitHub REST API.

---

## Auth Flow

1. **Login** — `GET /auth/github/login` generates a cryptographic `state` token, stores it in Redis with a 10-minute TTL, and redirects to GitHub's authorize page with scope `public_repo,read:user`.
2. **Callback** — `GET /auth/github/callback` validates the `state` against Redis (one-time use — consumed with `GETDEL`), exchanges the authorization code for a GitHub access token, fetches the user profile, upserts a `User` row with the access token encrypted at rest (Fernet), and issues a JWT.
3. **Redirect** — the JWT is passed to the frontend once via a `?token=` query parameter in the redirect URL to `FRONTEND_REDIRECT_URL`.
4. **Frontend** — the frontend reads the token from the URL, stores it in `sessionStorage`, and sends it on every API request as an `Authorization: Bearer <token>` header.
5. **Verification** — `get_current_user` (a FastAPI dependency) decodes the JWT, loads the `User` from the database, and returns 401 if anything fails.

JWTs are signed with HS256 and expire after 7 days (10 080 minutes, configurable via `JWT_EXPIRE_MINUTES`).

---

## API Reference

All routes are derived from the routers in `app/api/`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/health` | No | Liveness probe — returns `{"status": "ok"}` |
| `GET` | `/auth/github/login` | No | Redirect to GitHub OAuth authorize page |
| `GET` | `/auth/github/callback` | No | OAuth callback — exchange code, upsert user, issue JWT, redirect to frontend |
| `GET` | `/auth/github/me` | Yes | Return the authenticated user's public profile |
| `POST` | `/sync/github` | Yes | Run the full sync pipeline (repos → commits → daily snapshots), invalidate stats cache |
| `GET` | `/stats/summary` | Yes | Current streak, longest streak, weekly velocity (7 days), active repos (14 days) |
| `GET` | `/stats/heatmap` | Yes | Daily commit counts for the last 365 UTC calendar days |
| `GET` | `/stats/repos` | Yes | Per-repo commit activity for the last 30 days, sorted by commit count descending |
| `GET` | `/stats/activity-pattern` | Yes | All-time commit histograms by hour of day (UTC, 0–23) and day of week (Mon–Sun) |

---

## Project Structure

```
├── app/
│   ├── main.py                 # FastAPI app, CORS, router registration
│   ├── config.py               # pydantic-settings — all env vars
│   ├── db.py                   # Async SQLAlchemy engine + session factory
│   ├── cache.py                # Redis client singleton + cache invalidation
│   ├── dependencies.py         # get_current_user (JWT → User)
│   ├── api/
│   │   ├── auth.py             # GitHub OAuth login/callback/me routes
│   │   ├── sync.py             # POST /sync/github pipeline
│   │   └── stats.py            # Stats endpoints (summary, heatmap, repos, activity-pattern)
│   ├── models/
│   │   ├── base.py             # SQLAlchemy declarative base
│   │   ├── user.py             # User model
│   │   ├── repo.py             # Repo model (includes is_owner, owner_login)
│   │   ├── commit.py           # Commit model (unique on repo_id + sha)
│   │   └── daily_snapshot.py   # DailySnapshot model (unique on user_id + date)
│   ├── services/
│   │   ├── github_client.py    # Async HTTP client for GitHub REST API (paginated)
│   │   └── sync_service.py     # sync_repos, sync_commits, compute_daily_snapshots
│   └── utils/
│       ├── encryption.py       # Fernet encrypt/decrypt for access tokens
│       └── jwt.py              # JWT creation and validation (PyJWT)
├── alembic/                    # Alembic migration environment
│   └── versions/               # Migration scripts
├── frontend/
│   ├── index.html              # Single-page dashboard (auth + dashboard sections)
│   ├── package.json            # Vite + Chart.js
│   └── src/
│       ├── main.js             # Auth flow, API calls, Chart.js rendering
│       └── style.css           # Dashboard styles
├── scripts/                    # Integration test scripts (run in CI)
├── .github/workflows/ci.yml   # CI/CD — tests on ephemeral Neon branch + Docker build
├── Dockerfile                  # Multi-stage build (Python 3.13-slim)
├── docker-compose.yml          # Local dev — app + Redis
├── docker-entrypoint.sh        # Run alembic upgrade head, then exec uvicorn
├── requirements.txt            # Pinned Python dependencies
└── .env.example                # Template for required environment variables
```

---

## Local Setup

### Prerequisites

- Python 3.13+
- Node.js (for Vite)
- A PostgreSQL database (e.g. a [Neon](https://neon.tech) free-tier project)
- A Redis instance (local, or [Upstash](https://upstash.com) free tier)
- A **GitHub OAuth App** — create one at [github.com/settings/developers](https://github.com/settings/developers) with callback URL `http://localhost:8000/auth/github/callback`

### Backend

```bash
# 1. Clone the repository
git clone https://github.com/AdvaithBadam/Github-Activity-Analyst.git
cd Github-Activity-Analyst

# 2. Create a virtual environment and install dependencies
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

# 3. Copy the env template and fill in your values
cp .env.example .env

# 4. Run database migrations
alembic upgrade head

# 5. Start the backend
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend

# 1. Install dependencies
npm install

# 2. Set the backend URL (already defaults to http://localhost:8000)
#    Edit frontend/.env if your backend runs elsewhere:
#    VITE_BACKEND_URL=http://localhost:8000

# 3. Start the dev server
npm run dev
```

### Environment Variables

All backend env vars are defined in `app/config.py`. The frontend has a single env var in `frontend/.env`.

| Variable | Required | Default | Format / Notes |
|----------|----------|---------|----------------|
| `DATABASE_URL` | **Yes** | — | `postgresql+asyncpg://user:password@host:5432/dbname?ssl=require` (Neon requires `ssl=require`) |
| `GITHUB_CLIENT_ID` | **Yes** | — | From your GitHub OAuth App |
| `GITHUB_CLIENT_SECRET` | **Yes** | — | From your GitHub OAuth App |
| `GITHUB_REDIRECT_URI` | No | `http://localhost:8000/auth/github/callback` | Must match the callback URL in your OAuth App exactly |
| `ENCRYPTION_KEY` | **Yes** | — | Fernet key — generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `JWT_SECRET_KEY` | **Yes** | — | Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `JWT_ALGORITHM` | No | `HS256` | JWT signing algorithm |
| `JWT_EXPIRE_MINUTES` | No | `10080` (7 days) | JWT expiry in minutes |
| `REDIS_URL` | No | `redis://localhost:6379/0` | For production with TLS: `rediss://default:password@host:6379` |
| `FRONTEND_REDIRECT_URL` | No | `http://localhost:5173` | Where the backend redirects after OAuth (the frontend origin) |
| `FRONTEND_URL` | No | `http://localhost:5173` | Comma-separated list of allowed CORS origins |
| `VITE_BACKEND_URL` | No | `http://localhost:8000` | Frontend env var — the backend origin for all API calls |

> **Note:** Local development requires its own GitHub OAuth App with the callback URL set to `http://localhost:8000/auth/github/callback`.

---

## Deployment

### Backend — Render (Docker)

The backend is deployed on Render's free tier using the project's `Dockerfile`. The Docker entrypoint runs `alembic upgrade head` before starting uvicorn.

**Required env vars on Render:**

- `DATABASE_URL` — Neon connection string rewritten to `postgresql+asyncpg://` with `ssl=require`
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` — from the **production** GitHub OAuth App
- `GITHUB_REDIRECT_URI` — must match the production OAuth App callback URL exactly (e.g. `https://github-activity-analyst.onrender.com/auth/github/callback`)
- `ENCRYPTION_KEY`, `JWT_SECRET_KEY` — see generation commands above
- `REDIS_URL` — Upstash Redis URL (uses `rediss://` for TLS)
- `FRONTEND_REDIRECT_URL` — the production frontend URL (e.g. `https://github-activity-analyst.vercel.app`)
- `FRONTEND_URL` — same as above (for CORS)

### Frontend — Vercel

The frontend is a static Vite build deployed on Vercel from the `frontend/` directory.

**Required env var on Vercel:**

- `VITE_BACKEND_URL` — the production backend URL (e.g. `https://github-activity-analyst.onrender.com`)

---

## Testing and CI

### Test Scripts

Integration tests live in `scripts/` and run against a real database and the GitHub API:

| Script | What it tests |
|--------|---------------|
| `test_github_client.py` | GitHub REST API client (pagination, error handling) — no DB required |
| `test_sync_repos.py` | Repo upsert from GitHub → database |
| `test_sync_commits.py` | Incremental commit sync pipeline |
| `test_compute_daily_snapshots.py` | Daily snapshot aggregation from commits |
| `test_stats_summary.py` | Stats summary endpoint (streaks, velocity, active repos) |
| `test_cache_behavior.py` | Redis cache-aside behaviour and invalidation |
| `test_activity_pattern.py` | Hour-of-day and day-of-week activity pattern endpoint |
| `cleanup_test_repos.py` | Cleans up test data |

### CI Workflow (`.github/workflows/ci.yml`)

The `CI/CD` workflow runs on every push and pull request to any branch:

1. **Test job** — sets up Python 3.13 and a Redis service container, creates an ephemeral Neon database branch (`ci-<run_id>-<attempt>`), runs `alembic upgrade head`, then executes each test script sequentially. The Neon branch is unconditionally deleted in a cleanup step (even on failure).
2. **Build job** — runs after tests pass. Builds the Docker image using Docker Buildx with GitHub Actions cache. Does not push the image anywhere.

Concurrency is limited per workflow + ref to prevent parallel runs on the same branch.

---

## Design Decisions

1. **Bearer tokens instead of cookies** — the frontend (Vercel) and backend (Render) are on different domains. Browsers increasingly block third-party cookies in cross-origin requests, so the backend issues a JWT that the frontend stores in `sessionStorage` and sends as an `Authorization: Bearer` header.

2. **OAuth state in Redis, not a cookie** — for the same cross-domain cookie reason, the OAuth CSRF `state` parameter is stored server-side in Redis with a 10-minute TTL and consumed on use (`GETDEL`) to prevent replay.

3. **`pool_pre_ping` and `pool_recycle` on the SQLAlchemy engine** — Neon's serverless Postgres drops idle connections aggressively. `pool_pre_ping=True` tests connections before use, and `pool_recycle=300` proactively discards connections older than 5 minutes.

4. **One failed repo doesn't abort the sync** — if the GitHub API returns 404 for a repo (deleted, transferred, or scope gap), `sync_commits` logs a warning and skips that repo. The rest of the pipeline continues.

---

## Known Limitations

- **Private repos are invisible** — the OAuth scope is `public_repo`, so private repositories are not returned by the GitHub API at all (not merely forbidden).
- **Stale repo rows persist** — repos that are deleted, renamed, or made private on GitHub are not reconciled. Their rows remain in the database and 404 silently on the next commit sync.
- **365-day lookback on initial sync** — the first commit sync covers only the last 365 days of history. Older commits are never fetched.
- **`github_repo_id` is globally unique** — the `repos.github_repo_id` column has a unique constraint without a per-user scope. This works for the current single-user model but would need to be `(user_id, github_repo_id)` for multi-user.
- **`compute_daily_snapshots` doesn't zero stale rows** — if commits are deleted (e.g. force-pushed away), existing `DailySnapshot` rows for those days are not zeroed out; they retain the old count.
- **JWT in a query parameter** — after OAuth, the JWT travels in a `?token=` query parameter in the redirect URL. This could appear in server access logs and browser history.
- **Cold starts on free-tier hosting** — Render's free tier spins down after inactivity, so the first request after idle can take up to a minute.

---

## Future Improvements

- **Incremental daily snapshot updates** — instead of recomputing all snapshots on every sync, only update snapshots for days with new commits and zero out stale rows for deleted commits.
- **Private repo support** — request the `repo` scope (instead of `public_repo`) to include private repositories in the sync, with an opt-in toggle in the UI.
- **Webhook-driven sync** — register a GitHub webhook to trigger syncs on push events instead of requiring manual sync, keeping data fresh without polling.
- **Multi-user repo isolation** — change the `github_repo_id` unique constraint to `(user_id, github_repo_id)` to support multiple users tracking the same repo independently.