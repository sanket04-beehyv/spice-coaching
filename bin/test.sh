#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export DATABASE_URL_TEST="${DATABASE_URL_TEST:-postgresql+asyncpg://postgres:postgres@localhost:15433/microcoaching_test}"
export DATABASE_URL="${DATABASE_URL:-$DATABASE_URL_TEST}"
export REDIS_URL="${REDIS_URL:-redis://localhost:16380/0}"
export CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-localhost}"
export CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-18124}"

uv run pytest "$@"
