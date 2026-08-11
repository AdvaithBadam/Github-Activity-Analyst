# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

# Install build tools needed to compile C extensions (cffi/cryptography/asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the dependency manifest first so Docker cache is reused on code changes
COPY requirements.txt .

# Install all deps into a prefix directory so we can copy them cleanly
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# Non-root user
RUN useradd --no-create-home --shell /bin/false appuser

WORKDIR /app

# Pull in the installed packages from the build stage (no gcc, no libffi-dev)
COPY --from=builder /install /usr/local

# Copy application source
COPY alembic/       ./alembic/
COPY alembic.ini    .
COPY app/           ./app/

# Copy the entrypoint script and make it executable
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Run as non-root
USER appuser

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
