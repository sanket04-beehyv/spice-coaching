# W-10 Review Notes — Telemetry, Module Completion, Escalation

**Audience:** sr dev who built the v3.0 telemetry stack.
**Goal:** make this PR readable as "what changed and why," not "find the diff."

The v3.3 module pipeline (the AI-generated reviewable training modules)
introduces a **parallel** module-level telemetry path that runs alongside
your existing scenario-level path. Same FastAPI endpoint, same ClickHouse
writer, same Celery infra. Different identity (`module_family_id` UUID
vs `scenario_id` text), different completion semantics (passed quiz at
≥70% vs single answer recorded), different downstream tables.

---

## TL;DR — what reviewers need to know

| Touch | File | Change |
|---|---|---|
| Add | `mc_contracts/enums.py` | 3 new `CoachingEventType` values: `MODULE_DELIVERED`, `MODULE_CARD_VIEWED`, `MODULE_QUIZ_ATTEMPTED` |
| Add | `mc_contracts/telemetry.py` | 3 optional `TelemetryEvent` fields (`module_family_id: UUID`, `module_version: int`, `quiz_score_pct: float`) + 2 new `TelemetryAckResponse` lists (`duplicates`, `buffered`) + 500-event batch cap |
| Add | `services/quiz_evaluator.py` | Pure scoring function (correct/total/pct/passed + missed-question ids). No DB. |
| Add | `db/repositories/module_completion_repository.py` | CRUD for `chw_module_completion` (table from W-1). **Flush-only** — no commit inside the repo. |
| Add | `workers/module_completion_worker.py` | Consumes the 4 new event types; updates module-completion + behavioural-gap-state |
| Add | `services/telemetry_dedup.py` | Redis SET-NX dedup with 24h TTL |
| Add | `services/telemetry_buffer.py` | Redis-list retry queue for ClickHouse failures |
| Extend | `clickhouse/client.py` | Append `module_family_id`, `module_version`, `quiz_score_pct` to `_COACHING_EVENT_COLUMNS`. **Requires DDL on the cluster** (see "Operations" below) |
| Extend | `api/telemetry.py` | (a) dedup before any side-effect (b) ClickHouse failures buffer to Redis instead of 500 (c) new branch enqueues `process_module_event_task` for the 4 new event types |
| Extend | `celery_tasks.py` | One new task: `process_module_event_task` |
| Untouched | `services/gap_profile_service.py`, `workers/coaching_outcomes_worker.py`, `workers/digital_gaps_worker.py`, `workers/quiz_worker.py`, `workers/gap_profile_worker.py`, all existing Celery tasks | The legacy scenario flow keeps running |

**+50 tests, 620 passing total.**

---

## Why a parallel path (not a refactor)

Your v3.0 stack tracks **scenarios** (one card, one quiz answer, gap state per scenario_id). The v3.3 module pipeline produces **multi-card modules with multi-question quizzes**. Different identity and completion semantics:

| Dimension | v3.0 (your code) | v3.3 (this PR) |
|---|---|---|
| Identity | `scenario_id` (text) | `module_family_id` (UUID) |
| State table | `chw_gap_profile` (PK chw_id+scenario_id) | `chw_module_completion` (PK chw_id+module_family_id) — new in W-1 |
| Gap state | `chw_gap_profile.gap_active` | `chw_behavioural_gap_state` — new in W-1, updated via W-8's `GapStateService` |
| "Done" semantics | One quiz answer recorded | Quiz passed at ≥70% (default; per-module override is W-11) |
| Reinforcement | not built-in | `reinforcement_due_at = now + 90 days` on pass |
| Escalation | not in scope | ≥3 fails in 30 days → `escalated_to_supervisor` (uses your settings, see W-8 implementation) |

A unified path would have meant rewriting `gap_profile_service.py` to be polymorphic on identity type. We chose parallelism because (a) it leaves your tested code path untouched, (b) it's reversible if module-pipeline gets dropped, (c) the worker's reuse of W-8 `GapStateService` means we're not duplicating the gap-state machine.

---

## Three production-risk fixes that ride along

I audited the existing telemetry handler before extending it and found three things worth fixing while we were touching the same surface. Each is a real bug; not opinion-level. They benefit BOTH your scenario-level events and our new module-level events.

### Fix 1: Idempotency by event_id
**Bug:** ClickHouse is append-only. If the SDK retries a batch (network blip, app restart, ack-uncertain), every event in the batch becomes a duplicate row. `gap_profile_service` also gets called twice. Production risk: inflated metrics + double-incremented gap state.

**Fix:** `services/telemetry_dedup.py` — Redis SET-NX with 24h TTL keyed by event_id. The handler dedups in one pipelined round-trip before any side-effect, drops duplicates, returns them in the new `duplicates` list of the ack.

**Cost:** one Redis pipeline per batch (~ms).

### Fix 2: ClickHouse buffering instead of 500
**Bug:** When ClickHouse is unreachable, `await _ch_client.insert_coaching_events(...)` raises and the WHOLE batch is lost — the SDK sees a 500, retries the same batch repeatedly until it gives up, all events gone.

**Fix:** `services/telemetry_buffer.py` — wrap inserts in try/except that pushes failed rows to a per-table Redis list (`telemetry:retry:coaching_events`), capped at 50K rows. Handler returns 200 with `buffered` list; SDK treats them as ingested. A drain worker (deferred to next sprint or whenever ClickHouse stability is in question) reads the queue and retries. JSON encoding handles `date`/`datetime`/`UUID` row values.

**Cost:** zero on the happy path; bounded Redis memory in the bad path.

### Fix 3: 500-event batch cap
**Bug:** `TelemetryBatch` had no size limit. A device with a stuck loop or a malicious client could submit 1M events in one POST and OOM the server.

**Fix:** `Field(..., max_length=500)` on `TelemetryBatch.events`. Pydantic enforces at parse time; oversized batches return 422.

**Cost:** zero. Pilot batches are tens of events.

---

## Things I FLAGGED but did NOT fix unilaterally

These are your code, your call. Listed for your awareness so you decide whether to address them in a follow-up.

| # | Where | Issue |
|---|---|---|
| F1 | `api/telemetry.py` | **No authentication.** Anyone can post events as any chw_id. Same applies to the new W-9 sync endpoints. Likely deferred to W-12 (SDK contract research) but worth confirming. |
| F2 | `api/telemetry.py:124-126` | The `payload.get("success", payload.get("sucess"))` typo handler — added a tracking comment + TODO. Real fix is on the SDK side or a `event_schema_version >= N` cutover. |
| F3 | `db/repositories/gap_profile_repository.py:upsert` | Calls `await session.commit()` inside the repo — layering issue, blocks transaction-spanning tests. Our new `module_completion_repository.py` follows the flush-only convention used by the rest of the W-1/6/8 repos. |
| F4 | `celery_tasks.py:_get_celery_loop` | Single-loop-per-worker pattern with a thread guard. The `RuntimeError("Celery loop created on different thread...")` suggests a real bug was hit; the deeper fix (per-task fresh loop, or `aiocelery`) is unaddressed. |
| F5 | `event_type: CoachingEventType \| DigitalEventType \| str` | Backward-compat-by-string means typos silently bypass downstream branches. The handler at line 213 (`event_type_value in _MODULE_EVENT_TYPES`) inherits this issue but the new event types are still safe because `CoachingEventType.MODULE_*.value` is a constant. |
| F6 | ClickHouse schema migration | No tooling — DDL is run by hand on the cluster. Adding our new columns means an out-of-band ALTER (see "Operations" below). |
| F7 | Coverage | Zero unit tests for the existing handler. We added 11 tests for the handler that exercise BOTH the legacy scenario branch and the new module branch — your code is now tested as a side-effect of our PR. |

---

## Operations checklist before this lands

1. **ClickHouse DDL**: run on the cluster
    ```sql
    ALTER TABLE coaching_events
      ADD COLUMN IF NOT EXISTS module_family_id Nullable(String),
      ADD COLUMN IF NOT EXISTS module_version Nullable(Int32),
      ADD COLUMN IF NOT EXISTS quiz_score_pct Nullable(Float64);
    ```
    Order matches `_COACHING_EVENT_COLUMNS` in `clickhouse/client.py`.

2. **Redis keyspace**: 3 new prefixes are now used. No pre-creation needed — Redis creates on first write.
    - `telemetry:event:{event_id}` — dedup TTL keys (24h)
    - `telemetry:retry:coaching_events` — coaching-events retry list
    - `telemetry:retry:digital_events` — digital-events retry list

3. **Celery routing**: one new task `platform.process_module_event` is registered. Default queue. No new worker process required if existing workers consume the default queue.

4. **No migration on Postgres** — `chw_module_completion` was created in W-1 (alembic 0001 chain).

---

## Tests (50 new, all passing)

```
tests/services/test_quiz_evaluator.py          —  9 tests, pure-unit
tests/services/test_telemetry_dedup.py         —  5 tests, mocked Redis
tests/services/test_telemetry_buffer.py        —  7 tests, mocked Redis
tests/db/test_module_completion_repository.py  —  7 tests, real Postgres
tests/workers/test_module_completion_worker.py — 11 tests, real Postgres
tests/api/test_telemetry_handler.py            — 11 tests, FastAPI + mocks (3 backfill + 8 W-10)
```

The handler tests cover BOTH the legacy scenario flow and the new module
flow, so future regressions to your code are caught too.

---

## What's NOT in this PR

- **Drain worker for the retry buffer.** The buffer accumulates rows on
  ClickHouse outage; a worker consumes and re-inserts. The buffer is
  bounded at 50K rows and the drain worker is straightforward — deferred
  to keep this PR focused. Until it's added, a sustained outage will
  silently drop the 50,001st row onward.
- **Per-module pass-threshold override.** Spec mentions `Module.pass_threshold`
  for special-case modules. The default `settings.quiz_pass_threshold_default = 0.70`
  is used everywhere for now. W-11 scope.
- **Authentication on `/telemetry/events`.** Flag F1 above. W-12 scope.
- **Drain-worker observability.** Once the drain worker exists we'll want
  Prometheus metrics on `queue_depth` per table and an alert above N.

---

## Quick-look diff stats

```
 packages/contracts/src/mc_contracts/enums.py                         |  +13
 packages/contracts/src/mc_contracts/telemetry.py                     |  +30
 services/platform/src/platform_service/api/telemetry.py              | +130 -50
 services/platform/src/platform_service/celery_tasks.py               |  +12
 services/platform/src/platform_service/clickhouse/client.py          |  +12
 services/platform/src/platform_service/db/repositories/module_completion_repository.py | +136 (new)
 services/platform/src/platform_service/services/quiz_evaluator.py    | +120 (new)
 services/platform/src/platform_service/services/telemetry_buffer.py  | +110 (new)
 services/platform/src/platform_service/services/telemetry_dedup.py   |  +85 (new)
 services/platform/src/platform_service/workers/module_completion_worker.py | +220 (new)
 tests (6 new files)                                                  | +750 (new)
```

---

If you want to pair on any of the F1–F7 flagged items, let me know — happy
to do them but they touched conventions I didn't want to change without
your sign-off.
