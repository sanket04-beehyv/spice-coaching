FROM python:3.12-slim

WORKDIR /app

# ffmpeg + ffprobe drive the 2-minute AV splitter for transcript ingestion
# (services/platform/src/platform_service/workers/extractors/media_splitter.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy workspace files
COPY pyproject.toml /app/pyproject.toml
COPY packages/ /app/packages/
COPY services/platform/ /app/services/platform/

# Install platform service and its workspace dependencies
RUN uv pip install --system --no-cache \
    -e /app/packages/contracts \
    -e /app/packages/foundation \
    -e /app/services/platform

# Copy infra for migrations
COPY infra/ /app/infra/
COPY seed/ /app/seed/

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "platform_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
