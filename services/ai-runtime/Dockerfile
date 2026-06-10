FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Copy workspace files
COPY pyproject.toml /app/pyproject.toml
COPY packages/ /app/packages/
COPY services/ai-runtime/ /app/services/ai-runtime/

# Install ai-runtime and its workspace dependencies
RUN uv pip install --system --no-cache \
    -e /app/packages/contracts \
    -e /app/packages/foundation \
    -e /app/services/ai-runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8001

CMD ["uvicorn", "ai_runtime.main:app", "--host", "0.0.0.0", "--port", "8001"]
