FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy workspace files (all lockfile members required for locked workspace sync)
COPY pyproject.toml uv.lock /app/
COPY packages/ /app/packages/
COPY services/ /app/services/

RUN uv sync --locked --no-dev --package ai-runtime
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8001

CMD ["uvicorn", "ai_runtime.main:app", "--host", "0.0.0.0", "--port", "8001"]
