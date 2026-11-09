# Multi-user nutrition server. Railway builds this with the service's root
# directory set to multi-user-server/.
FROM python:3.14-slim

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Exact versions from uv.lock; no test tools in the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Must run as ONE instance: paused two-factor logins live in memory.
CMD ["nutrition-server"]
