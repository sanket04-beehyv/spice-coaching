FROM python:3.12-slim

WORKDIR /app

# ffmpeg + ffprobe drive the 2-minute AV splitter for transcript ingestion
# (services/platform/src/platform_service/workers/extractors/media_splitter.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy workspace files (all lockfile members required for locked workspace sync)
COPY pyproject.toml uv.lock /app/
COPY packages/ /app/packages/
COPY services/ /app/services/

RUN uv sync --locked --no-dev --package microcoaching-platform
ENV PATH="/app/.venv/bin:$PATH"

# Copy infra for migrations
COPY infra/ /app/infra/
COPY seed/ /app/seed/

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "platform_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
