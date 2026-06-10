# Test Infra — Deferred

W-12 has its own document now: see [W12_SDK_INTEGRATION.md](W12_SDK_INTEGRATION.md).
This file used to cover both; the W-12 section was removed once we got
SPICE repo access (commit `5043a97` on `uhis-dev`).

What's left is the test-infra workstream, which is still deferred.

---

## Why deferred

Test patterns will harden naturally as W-12 and the Phase-1 audit work
land. Doing the test-infra workstream first means re-doing it after the
SDK contract changes — we'd rather formalise the patterns once, against
the final shape.

## What's currently working

- 723 tests passing, including a healthy mix of:
  - Pure-unit tests (validators, evaluators, parsers) — no external deps
  - DB-integration tests (repos, workers) — gated by `DATABASE_URL_TEST`
  - API tests (httpx + ASGI transport, mocked externals)
  - End-to-end pipeline tests with real stage classes + mocked LLM
- `tests/conftest.py` aligns `DATABASE_URL` with `DATABASE_URL_TEST`,
  runs `alembic upgrade head` once per session, and provides a
  `db_session` fixture that shares the engine with `SessionLocal`.
- All DB-touching tests use the `requires_db` marker.
- ~10 test files added in W-10 / W-11 / W-12 / Phase 0 with consistent
  fixtures + builders.

## What the workstream will deliver (when run)

1. **`docker-compose.test.yml`** — pg + clickhouse + redis + ai-runtime
   mock, `make test-up` / `make test-down`. Removes the manual
   `docker run pgvector/pgvector:pg15` setup.
2. **CI workflow** — GitHub Actions (or whichever CI is in use) running
   the full suite on PR against the compose fixture.
3. **Shared `tests/fixtures/` module** — common builders for
   `BehaviouralGap`, `SourceDocument`, `ModuleFamily`, `Module`,
   `ChwModuleCompletion`, etc. Right now each test file rebuilds these
   inline (we hit a bug once because of differing field names: see the
   `BehaviouralGap(code=..., label_en=...)` mistake fixed in the W-6 PR).
4. **Per-test transaction isolation** — current pattern uses
   `expire_on_commit=False` + manual rollback or per-suite `TRUNCATE`
   fixtures; some tests commit explicitly which leaks state. A
   SAVEPOINT-based isolation wrapper would fix this.
5. **`bin/test.sh`** — wraps `DATABASE_URL_TEST=… uv run pytest` so
   contributors don't have to remember the env vars.
6. **Separate test database** — `DATABASE_URL_TEST` currently points at
   the same DB as production by default, so running the test suite
   wipes ingested production data via the autouse `TRUNCATE` fixtures.
   Provision a dedicated `microcoaching_test` database in the compose
   file and default the test config to it.
7. **`docs/TESTING.md`** — convention guide: when to use `requires_db`,
   when to mock vs use real Redis, fixture composition, etc.

## Estimated effort once started

1–2 days. Most of the patterns already exist in the suite; this is
formalising them, not designing from scratch.

The most urgent item is #6 — separate test database. The wipe-prod-data
hazard is real and has bitten us once during the SK-PDF smoke run.

---

## Bringing this back

When we're ready (after the SK-PDF smoke verifies the consolidation
prompt, and after W-12.1 freezes the slot spec):

1. Item #6 first (separate test DB) — half a day, immediate safety win.
2. Items #1, #2, #5 — half a day, formalises the dev loop.
3. Items #3, #4, #7 — one day, paves the road for pilot stability work.
