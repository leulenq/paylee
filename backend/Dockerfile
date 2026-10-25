FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

RUN useradd --create-home paylee && mkdir -p /srv/uploads && chown paylee /srv/uploads
USER paylee

ENV ENVIRONMENT=production UPLOAD_DIR=/srv/uploads
EXPOSE 8000

# One process: the rate limiters and the email worker live in memory. Scale with more containers only after
# moving rate limits to a shared store (the email outbox is already safe to run in several processes).
CMD ["/srv/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
