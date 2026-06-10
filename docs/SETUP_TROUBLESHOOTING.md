# Setup Troubleshooting

This guide captures setup issues observed while running `docker compose up` for local development.

## Prerequisites (do these once)

```bash
cp .env.example .env           # then fill GOOGLE_API_KEY
uv sync --all-packages --group dev
```

If you skip the `.env` copy, `docker compose up` fails at parse time with:
`required variable GOOGLE_API_KEY is missing a value`.

## Known Issues and Fixes

### 1) Docker Compose warning: `version` is obsolete

**Symptom**
- Compose prints: `` `version` is obsolete ``.

**Root Cause**
- Docker Compose v2 no longer requires the top-level `version` field.

**Fix Applied**
- Removed `version` from `docker-compose.yml`.

**How to Verify**
- Run `docker compose up`.
- Confirm there is no `version is obsolete` warning.

---

### 2) `platform-api` and `ai-runtime` healthchecks failed — "curl: not found"

**Symptom**
- Containers started but were stuck `(health: starting)` indefinitely.
- `docker inspect <container> --format '{{json .State.Health}}'` showed `"curl: not found"` or exit code `127`.
- Dependent services never started because their `depends_on: condition: service_healthy` was never satisfied.

**Root Cause**
- Both service Dockerfiles use `python:3.12-slim` as the base image. That image does **not** include `curl`. The prior healthcheck (`curl -f http://localhost:.../health || exit 1`) therefore always failed.

**Fix Applied**
- Healthchecks now use a zero-dependency Python probe (no shell quoting):
  ```yaml
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/medtronics-api/health')"]
  ```
- Applied consistently to both `platform-api` and `ai-runtime`.

**How to Verify**
- `docker compose up`.
- `docker compose ps` — both services should report `(healthy)` within ~30 seconds.
- `platform-api` and `platform-worker` transition to running after their dependencies are healthy.

**Preventive rule**
- Do not add healthchecks that depend on `curl`, `wget`, or `psql` to containers built on `python:*-slim`. Either install the tool in the Dockerfile or use the Python probe pattern above.

---

### 3) Redis warning about `vm.overcommit_memory`

**Symptom**
- Redis prints host warning:
  - `Memory overcommit must be enabled!`

**Impact**
- Usually non-blocking in local development.
- Can affect Redis reliability under memory pressure.

**Optional Host Fix (Linux)**
```bash
sudo sysctl vm.overcommit_memory=1
```

To persist across reboots, add `vm.overcommit_memory = 1` to `/etc/sysctl.conf`.

---

### 4) AI runtime provider warning about deprecated Google SDK

**Symptom**
- Runtime logs show deprecation warning for `google.generativeai`.

**Impact**
- Service still starts and responds to healthchecks.
- Indicates technical debt and future migration risk.

**Recommended Follow-up**
- Track migration from `google.generativeai` to `google.genai` in `services/ai-runtime`.

---

### 5) Alembic `DATABASE_URL` failures in local shells

**Symptom**
- Running Alembic manually fails with `DATABASE_URL is not set`.

**Root Cause**
- Local shell may not export `DATABASE_URL`, and migration env now intentionally requires it.

**Fix Applied**
- `infra/alembic/env.py` now:
  - loads `.env` with `python-dotenv`,
  - requires `DATABASE_URL` explicitly (fails fast if missing).

**How to Verify**
```bash
alembic -c infra/alembic.ini upgrade head
```
- Confirm migration runs when `DATABASE_URL` is present in environment or `.env`.

---

### 6) Stale `migrate` image after pulling new migrations

**Symptom**
- New migrations exist in `infra/alembic/versions/` but `docker compose up` reports the migrate container exited cleanly without applying them.
- Tables expected by newly-merged code are missing from the DB; platform-api fails with `relation "..." does not exist`.

**Root Cause**
- `docker compose up` does **not** rebuild service images. The `migrate` service image is built once and reused. When new migration files land, the cached image still contains only the older revisions.

**Fix Applied (operational, not in-repo)**
- After pulling new migrations, rebuild the migrate image and run it explicitly:
  ```bash
  docker compose build migrate
  docker compose run --rm migrate alembic -c infra/alembic.ini upgrade head
  ```

**Preventive rule**
- Treat a migration merge as a build-then-run event, not a `compose up` event. CI deployment scripts should do `compose build migrate && compose run --rm migrate ...` before bringing the rest of the stack up.

---

### 7) Migrate fails: `Can't locate revision identified by '0013'`

**Symptom**
- `coaching-platform-migrate-1` exits with code `255`.
- Logs show: `Can't locate revision identified by '0013'`.

**Root Cause**
- The Postgres volume was migrated on another branch (e.g. `optimal-ingest`, `evaluation-framework`) whose Alembic chain uses revisions `0009`–`0013`. The current branch (`module-generation-changes`) only ships revisions `0001`–`0008` with a different history. Alembic cannot find revision `0013` in the image.

**Fix (keep existing data)**
- If the DB already has the expected tables (`module`, `file_upload`, etc.), stamp the DB to this branch's head and re-run migrate:
  ```bash
  docker compose exec db psql -U postgres -d microcoaching \
    -c "UPDATE alembic_version SET version_num = '0008';"
  docker compose run --rm migrate alembic -c infra/alembic.ini upgrade head
  docker compose up -d
  ```

**Fix (fresh local DB)**
- Reset the volume when you do not need existing module data:
  ```bash
  docker compose down -v
  docker compose up -d
  ```

## Recommended Startup Sequence

```bash
docker compose down
docker compose up
```

Wait until:
- `migrate` exits with code `0`.
- `platform-api` reports startup complete on port `8000`.
- `platform-worker` reports queue listeners.
- `ai-runtime` health checks report `200 OK`.

### 7) Ingest returns `batch_queued` but pipeline never progresses

**Symptom**
- `POST /admin/ingest` returns `202` with `batch_queued`, but `GET /admin/ingest/by-document/{id}` never shows a running `ingestion_run`.

**Root Cause**
- Pipeline work runs on `platform-celery-worker`, not inside `platform-api`. The worker must be running and able to reach MinIO (ingest objects are downloaded from object storage during Stage A).

**Fix**
- Confirm `docker compose ps` shows `platform-celery-worker` up.
- Check worker logs: `docker compose logs platform-celery-worker`.
- Ensure the worker has the same `minio_*` settings as `platform-api` (see `docker-compose.yml`).

---

## Quick Validation Checklist

- `http://localhost:8000/medtronics-api/health` returns OK.
- `http://localhost:8001/health` returns OK.
- `http://localhost:8000/docs` loads OpenAPI docs.
