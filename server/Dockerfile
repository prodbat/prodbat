# Prodbat server: the web server and the worker in one container.
# It needs a Postgres database, given as PRODBAT_DATABASE_URL.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first, so they are cached when only the code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY prodbat_server ./prodbat_server
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# Run as an ordinary user, not root.
RUN useradd --system --uid 10001 --no-create-home prodbat
USER prodbat

# Creates or updates the tables, then serves and watches.
CMD ["python", "-m", "prodbat_server", "run"]
