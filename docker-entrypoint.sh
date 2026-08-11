#!/bin/sh
# docker-entrypoint.sh — run migrations then start the server.
# Executed as ENTRYPOINT inside the container (as appuser).
#
# Exit behaviour:
#   - If alembic upgrade head fails, print a clear error and exit non-zero.
#     The server is NOT started on a failed migration.
#   - On success, exec into uvicorn (replaces this shell — PID 1 stays uvicorn).

set -e

echo "[entrypoint] Running alembic upgrade head..."
if ! alembic upgrade head; then
    echo "[entrypoint] ERROR: alembic upgrade head failed — aborting startup." >&2
    exit 1
fi
echo "[entrypoint] Migrations applied successfully."

echo "[entrypoint] Starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
