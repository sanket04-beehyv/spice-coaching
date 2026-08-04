# Telemetry Contract — `/telemetry/events`

**Audience:** SDK devs (Android), backend devs touching ingest, dashboard analytics.
**Status:** Drafted on `module-generation-changes` ahead of telemetry-branch convergence. The body describes the **post-merge target state** — the field tables and enum values reflect what the SDK should send once the small contract diffs (covered in this doc) land on the telemetry branch. The only code-side change carried with this doc is the `TriggerType` widening in `enums.py`. The `TelemetryEvent` fields `module_id`, `card_family_id`, `quiz_id` exist on the telemetry branch but not yet here; the doc describes the unified contract that emerges once both branches reach `main`.
**Source files:** `packages/contracts/src/mc_contracts/telemetry.py`, `packages/contracts/src/mc_contracts/enums.py`, `services/platform/src/platform_service/api/telemetry.py`, `infra/clickhouse/init.sql`.

This doc is the canonical reference for one thing: **what the SDK posts to `POST /telemetry/events` and what the backend does with it.** Anything else (pipeline architecture, SDK lifecycle hooks, gap-state semantics) belongs in `ARCHITECTURE_RESET.md` / `W12_SDK_INTEGRATION.md` / `W10_REVIEW_NOTES.md`. Cross-references at the end.

---

## TL;DR

- Endpoint: `POST /telemetry/events` accepts a `TelemetryBatch` of up to 500 events.
- Two ClickHouse sinks: `coaching_events` (default) and `digital_events` (when `event_family == "digital"`).
- Two Celery side-channels: `process_module_event_task` (v3.3 module events) and `process_gap_update_task` (legacy scenario events). Both fire after ClickHouse insert; ClickHouse outage doesn't block them.
- Idempotent on `id` (Redis SET-NX, 24h TTL). Failed inserts buffer to a Redis retry queue, not a 5xx.
- **The SDK sends raw observations. The backend computes verdicts** (compliance, correctness, completion). See "Architectural rules."

---

## Envelope — `TelemetryBatch`

```json
{
  "sdk_version": "1.4.2-android",
  "chw_id": 123456789,
  "tenant_id": "<UUID|null>",
  "events": [ /* 1..500 TelemetryEvent objects */ ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `sdk_version` | string | yes | Free-form; surfaced in ClickHouse for triage |
| `chw_id` | integer (JSON number) | yes | SPICE CHW identifier |
| `tenant_id` | UUID | no | SPICE tenant identifier; nullable for non-tenanted events |
| `events` | TelemetryEvent[] | yes | Max 500 per batch (`_MAX_EVENTS_PER_BATCH`) |

**Do not** put `sdk_version`, `chw_id`, or `tenant_id` on individual events — they live on the batch. Repeating them per-event is wasted bandwidth and a source of inconsistency.

---

## Event — `TelemetryEvent`

### Identity & session

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | SDK-generated UUID. Backend dedups on this. **Stable across SDK retries** (worker reuses the same UUID; do not mint a new one on retry). |
| `event_schema_version` | int | default 1 | Bump on breaking schema changes. |
| `session_id` | string \| null | no | Free-form session identifier (e.g. `sess-<uuid>`) |
| `event_date` | date (`YYYY-MM-DD`) | yes | Local-calendar date the event occurred |
| `timestamp_local` | int | yes | Local epoch seconds |
| `timestamp_utc` | int \| null | no | UTC epoch seconds; backend will derive if absent |

### Patient / encounter (only for `clinical_observed` family)

| Field | Type | Notes |
|---|---|---|
| `patient_id_hash` | string \| null | **SHA-256 hex of SPICE `patientId`.** Raw `patientId` MUST NOT cross the SDK boundary. |
| `patient_visit_id` | string \| null | SPICE encounter id |
| `patient_track_id` | string \| null | Reserved; not yet used |
| `village_id` | string \| null | SPICE generic `villageId` (= Bangladesh "Union") — see Geo Hierarchy below |
| `upazila_id` | string \| null | SPICE generic `chiefdomId` (= Bangladesh "Upazila") |

### Classification — pick one event_family/event_type combo

| Field | Type | Required | Notes |
|---|---|---|---|
| `event_family` | EventFamily enum | yes | **Strictly enum-validated**, not string-fallback. Sending an unknown family returns 422. |
| `event_type` | enum \| string | yes | String-fallback is allowed for forward compatibility, but unknown values won't trigger any backend processing — they just land in ClickHouse. |

Allowed `event_family` values (`mc_contracts/enums.py:EventFamily`):

- `learning` — generic learning interactions (sessions, sync events)
- `coaching` — card / quiz / module surface events
- `clinical_observed` — events describing what the CHW did with a patient
- `digital` — SPICE app proficiency events (sync, login, form submit, help)
- `system` — SDK self-instrumentation (errors, crashes)

### Module / card / quiz identifiers

These fields disambiguate *which content* an event refers to. Set on `coaching` and `clinical_observed` family events as relevant.

| Field | Type | Notes |
|---|---|---|
| `module_family_id` | string (UUID) \| null | Stable across module versions. Use this for cross-version analytics aggregation. |
| `module_id` | string (UUID) \| null | Identifies the specific module version (`module.id` row). Use when a single version's metrics matter. |
| `module_version` | int \| null | Version number on the family. Useful as a discriminator when `module_id` isn't sent. |
| `card_family_id` | string (UUID) \| null | For per-card analytics. Send the persisted `card_family_id` from the sync bundle card payload (relational `module_card` table). Optional positional detail: `card_order` in `payload_json`. |
| `quiz_id` | string (UUID) \| null | `module_quiz_question.id` for the question being answered on `module_quiz_attempted`. |
| `quiz_score_pct` | float `[0.0, 1.0]` \| null | **Required on `module_quiz_attempted`.** Range 0.0–1.0, NOT 0–100. Pydantic enforces. |

**On the `_id` vs `_family_id` choice:** when in doubt, send the family id. Family-level aggregation is what the dashboard charts default to; sending only `module_id` forces a JOIN. Sending both is fine and recommended for v3.3 events.

### Generation context (LLM-bound events only)

| Field | Type | Notes |
|---|---|---|
| `inference_mode` | InferenceMode enum \| null | `online`, `edge`, `cached`, `unknown`. **Only meaningful for LLM-generated content** (chat responses, on-the-fly card generation). For pre-rendered cards from the W-9 sync bundle, leave null. |
| `validator_status` | ValidatorStatus enum \| null | `pass` / `fail` / `warn` / `fallback` / `unknown`. Set when the SDK ran the on-device output validator. |
| `fallback_used` | bool \| null | True if the SDK used a cached fallback because the live generation failed. |

### Free-form payload

| Field | Type | Notes |
|---|---|---|
| `payload_json` | object | default `{}` | Event-specific raw inputs. **This is where uncategorised structured data goes** — including the per-question answers on a quiz_attempted, the rule-engine recommendation on a clinical event, etc. The backend reads keys it knows about and ignores the rest. |

### Other fields

| Field | Type | Notes |
|---|---|---|
| `clinical_domain` | ClinicalDomain enum \| null | `hypertension`, `diabetes`, `maternal_health`, `emergency`, `spice_digital`. Optional on the wire; the backend may stamp this server-side from `module_family_id` if you leave it null. |
| `card_type` | CardType enum \| null | `info`, `action`, `quiz`, `observation`, `unknown`. Mostly redundant with `event_type` for module-pipeline events; useful for legacy scenario events. |
| `trigger_type` | TriggerType enum \| null | What caused this event. `hard` / `soft` (rule strength, legacy scenario flow); `morning` (morning-briefing surface); `gap` (driven by an identified behavioural gap); `workflow_event` (fired by a SPICE lifecycle hook); `user_action` (CHW explicitly opened a tile/card); `unknown`. The two axes (strength vs causal source) are mixed for backward compatibility — pick the value that best describes the trigger. |
| `outcome` | Outcome enum \| null | `correct`, `wrong`, `incorrect`, `skip`, `unknown`. Set on quiz events. **Do NOT set on `clinical_observed` events** — backend computes correctness; see Architectural rules. |
| `network_state` | string \| null | Free-form (`online` / `offline` / `weak`). |

---

## Routing rules

What the ingest endpoint does with each event (`api/telemetry.py:ingest_events`):

```
batch arrives
  ├─ dedup on `id` via Redis SET-NX (24h TTL)         ← duplicates returned in `duplicates` ack
  └─ for each first-seen event:
       ├─ append to coaching_events insert batch
       │
       ├─ if event_type == "module_requested"
       │      AND (module_id present OR non-empty payload_json.requested_module_name)
       │    └─ enqueue process_training_request_event_task
       │         (creates chw_training_request + individual assignment; no learning points)
       │
       ├─ if event_type == "video_progress_updated"
       │      AND payload_json.source_document_id present
       │    └─ enqueue process_video_progress_event_task
       │         (monotonic upsert into chw_video_progress; no learning points)
       │
       ├─ if event_type == "document_viewed"
       │    └─ (no Celery side-channel — ClickHouse + document_view_daily MV only)
       │
       ├─ if gap mode AND event_type ∈ {module_delivered, module_card_viewed,
       │                           module_quiz_attempted} AND module_id present
       │    └─ enqueue process_module_event_task   (v3.3 module path)
       │
       ├─ if gap mode AND event_type == spice_action_observed
       │    └─ enqueue process_module_event_task
       │
       └─ if quiz mode AND event_type == module_quiz_attempted AND module_id present
            └─ enqueue process_module_event_task
ClickHouse inserts (best-effort):
  └─ on failure → push rows to Redis retry queue, return them in `buffered` ack
```

**Celery jobs are NOT gated on the ClickHouse insert succeeding** — operational state (completion / training requests / video progress) shouldn't be held hostage to an analytics outage.

Invalid modules and duplicate training requests are **no-ops** inside `process_training_request_event_task` (logged); the ingest ACK still lists the event as accepted.

### CHW learning points (Postgres)

For CHWs identified by batch-level `chw_id`, the same `process_module_event_task` worker inserts rows into **`chw_learning_point_event`** (one row per scored telemetry `id`, with a **`points`** delta) when it accepts these `event_type` values: **`module_delivered`**, **`module_card_viewed`**, **`module_quiz_attempted`**, and **`spice_action_observed`**. The CHW's total is **`SUM(points)`** for that `chw_id` (indexed). Point amounts are configurable in platform settings (`learning_points_*`); **`module_quiz_attempted`** adds a score-based bonus from `quiz_score_pct` (0.0–1.0). **`module_requested` does not award learning points.**

Idempotency is enforced by the **`event_id` primary key** on `chw_learning_point_event`: re-sending the same telemetry UUID does not insert again, so totals do not double-count — **the SDK must keep `id` stable across retries** (consistent with Redis ingest dedup).

## Allowed `event_type` values

### `coaching` family — `CoachingEventType`

| Value | Used for | Routes to |
|---|---|---|
| `card_shown` | Legacy scenario card displayed | coaching_events |
| `card_accepted` | CHW marked card "useful" | coaching_events |
| `card_skipped` | CHW dismissed without reading | coaching_events |
| `audio_played` | Audio narration played | coaching_events |
| `quiz_started` | Legacy quiz opened | coaching_events |
| `quiz_answered` | Legacy quiz answer submitted | coaching_events |
| `counselling_used` | Counselling card invoked | coaching_events |
| `spice_action_observed` | **SPICE workflow event observed via lifecycle hook — assessment submitted, referral submitted, vital threshold crossed, etc.** Use `payload_json.kind` to discriminate (`assessment_submitted` / `referral_submitted` / etc.). One enum value covers all SPICE-workflow observations so future variants don't need enum changes. | coaching_events + `process_module_event_task` (gap state + **learning points**) |
| `risk_flag_observed` | Rule engine surfaced a risk flag | coaching_events |
| `equipment_anomaly_observed` | Vital outside expected range | coaching_events |
| `session_start` / `session_end` | SDK session boundaries | coaching_events |
| `sync_started` / `sync_completed` | Inbound sync worker boundaries | coaching_events |
| **`module_delivered`** | v3.3 module surfaced in morning rotation | coaching_events + `process_module_event_task` (**learning points**) |
| **`module_card_viewed`** | v3.3 card opened within a module | coaching_events + `process_module_event_task` (**learning points**) |
| **`module_quiz_viewed`** | v3.3 module quiz surface opened | coaching_events |
| **`module_quiz_attempted`** | v3.3 module quiz finished (carries `quiz_score_pct`) | coaching_events + `process_module_event_task` (completion + gap + **learning points**) |
| **`module_requested`** | CHW requested access to a published module (`module_id`) and/or a free-text custom name (`payload_json.requested_module_name`); optional `payload_json.reason` | coaching_events + `process_training_request_event_task` (training request + assignment; **no** learning points / gap) |
| **`video_progress_updated`** | CHW watch progress for an assigned video (`payload_json.source_document_id`, `last_position_ms`, `percent_watched` 0–100, optional `completed`) | coaching_events + `process_video_progress_event_task` (monotonic upsert of `chw_video_progress`; **no** learning points / gap). Appears on next `GET /sync/assigned-videos` |
| **`document_viewed`** | User viewed a knowledge `source_document` (PDF / pptx / docx / audio / video / other). Required `payload_json.source_document_id`. Batch `chw_id` = viewer SPICE user id. Mint a **new** event `id` per view so repeats count. | coaching_events + `document_view_daily` MV. **No** Celery side-effects, **no** learning points / gap. |

### `digital` family — `DigitalEventType`

| Value | Routes to |
|---|---|
| `sync_attempt` | digital_events |
| `login_attempt` | digital_events |
| `form_submit` | digital_events |
| `digital_help_used` | digital_events |

For digital events, the `payload_json` may include `success: bool` and `error_type: string` — the ingest extracts these into dedicated columns. (Note: legacy SDKs sometimes ship a `sucess` typo; the ingest tolerates it.)

**Chat usage** is a UX-quality signal, not a compliance signal — chat is a delivery surface for content the CHW already has via modules. For now, treat any chat usage as `digital_help_used`. If lightweight chat-usage analytics becomes useful later, add `chat_query_made` to `DigitalEventType` and route through this same `digital_events` path. **Do not give chat its own `event_family`** — the existing routing handles it cleanly.

---

## Worked examples

### `module_card_viewed`

A CHW opens a card within a module they're working through.

```json
{
  "id": "8a1c4f0e-2b6e-4f7e-8d5a-1e3f9c2d4b6a",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "coaching",
  "event_type": "module_card_viewed",
  "module_family_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e",
  "module_id": "8a4b2c19-1234-5678-9abc-def012345678",
  "card_family_id": "f0a1b2c3-d4e5-6789-abcd-ef0123456789",
  "module_version": 1,
  "trigger_type": "morning",
  "network_state": "online",
  "payload_json": {"card_order": 3},
  "event_date": "2026-04-28",
  "timestamp_local": 1714305600,
  "timestamp_utc": 1714283400
}
```

### `module_quiz_attempted`

CHW finishes the module quiz. `quiz_score_pct` is required on this event type.

Postgres module completion (`chw_module_quiz_progress` / `chw_module_completion`) is driven by per-question **attempts** (`quiz_id` on each event), not by `outcome`. `outcome` still affects behavioural-gap state and learning points.

```json
{
  "id": "9b2d5e1f-3c7f-5a8b-9c6d-2f4a0d3e5c7b",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "coaching",
  "event_type": "module_quiz_attempted",
  "module_family_id": "6375c1ba-53a7-44a2-9c4c-1c772126f32e",
  "module_id": "8a4b2c19-1234-5678-9abc-def012345678",
  "quiz_id": "f0875a8f-edfa-425c-b192-941b83d58def",
  "module_version": 1,
  "quiz_score_pct": 0.83,
  "outcome": "correct",
  "payload_json": {
    "selected_options": [1, 2]
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714305900,
  "timestamp_utc": 1714283700
}
```

### `module_requested`

CHW requests access to a training module. Provide either top-level `module_id` (published module) or `payload_json.requested_module_name` (free-text custom request), or both. Optional `payload_json.reason`.

Ingest always writes the row to ClickHouse. When at least one identity field is present, it enqueues `process_training_request_event_task`, which creates `chw_training_request` and (for a valid published `module_id`) an individual `chw_module_assignment`. Invalid / duplicate requests are logged no-ops after ACK.

Accepted requests appear on the next `GET /sync/modules?user_id=...` under `requested_modules` (full history for that CHW, separate from `assigned_module_ids`). Custom-name-only requests are included even though they do not create an assignment.

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "coaching",
  "event_type": "module_requested",
  "module_id": "8a4b2c19-1234-5678-9abc-def012345678",
  "payload_json": {
    "reason": "Need refresher before field visits"
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306000,
  "timestamp_utc": 1714283800
}
```

Free-text-only example (no `module_id`):

```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "event_family": "coaching",
  "event_type": "module_requested",
  "payload_json": {
    "requested_module_name": "Diabetes Counseling Refresh",
    "reason": "Need support for new patient cases"
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306100
}
```

### `video_progress_updated`

Device reports watch progress for an assigned video (buffered offline and flushed with other telemetry). Required `payload_json` keys: `source_document_id` (UUID of the video `source_document`), `last_position_ms` (≥ 0), `percent_watched` (0–100). Optional `completed` (default false).

Ingest writes ClickHouse and enqueues `process_video_progress_event_task`, which monotonically upserts `chw_video_progress` (`percent_watched` / `last_position_ms` never regress; `completed` sticks once true). Progress appears on the next `GET /sync/assigned-videos`.

```json
{
  "id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "coaching",
  "event_type": "video_progress_updated",
  "payload_json": {
    "source_document_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "last_position_ms": 125000,
    "percent_watched": 42.5,
    "completed": false
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306200,
  "timestamp_utc": 1714284000
}
```

### `document_viewed`

Mobile and admin/web clients emit this when a user views a knowledge document from the Learning Library (or equivalent). Use a **fresh** event `id` for every view — the same user reopening the same document must increment view counts.

Required `payload_json` keys: `source_document_id` (UUID of the `source_document` row). Optional denormalized `document_title` may be sent for offline triage but dashboards enrich title from PostgreSQL and **must not** trust the client value.

Set `upazila_id` (SPICE `chiefdomId`) when known. Batch `chw_id` is the viewer’s SPICE user id (field workers and admin principals alike).

Ingest writes ClickHouse only — **no** Celery worker, **no** learning points, **no** Postgres counter. Analytics rollups use the `document_view_daily` materialized view; drill-down reads raw `coaching_events`. Dashboard API: `GET /dashboard/document-usage` (KPIs + documents + events in one response).

```json
{
  "id": "d4e5f6a7-b8c9-0123-defa-234567890123",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "coaching",
  "event_type": "document_viewed",
  "upazila_id": "upazila-gazipur-sadar",
  "trigger_type": "user_action",
  "payload_json": {
    "source_document_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306300,
  "timestamp_utc": 1714284100
}
```

**Client handoff (follow-up tickets):** both the mobile SDK and the admin/web app must emit `document_viewed` on view. Until they do, dashboard metrics stay empty. Retain one event UUID per view across retries (Redis ingest dedup).

### `digital_help_used`

CHW invoked the in-app chat experience.

`payload_json` keys:

| Key | Type | Notes |
|-----|------|-------|
| `question` | string | **Required for FAQ mining.** Free-text query the CHW typed. Fallback key: `query`. |
| `topic` | string | Optional coarse label (e.g. `blood_pressure_cuff`). |
| `success` | bool | Whether the chat interaction succeeded. |
| `error_type` | string | Optional failure discriminator when `success` is false. |

**Nightly FAQ pipeline:** Platform aggregates `question` values from `digital_help_used`
events, semantically clusters paraphrases via embeddings, and uses an LLM to synthesize
5–6 bilingual FAQ suggestion chips per tenant. Devices sync the synthesized FAQs via
`GET /sync/chat-faqs` — they are **not** raw telemetry strings.

```json
{
  "id": "bc4f7a3b-5e9b-7c0d-be8f-4b6c2f5a7e9d",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "event_family": "digital",
  "event_type": "digital_help_used",
  "inference_mode": "edge",
  "validator_status": "pass",
  "fallback_used": false,
  "network_state": "offline",
  "payload_json": {
    "question": "How do I measure respiratory rate in a child?",
    "topic": "respiratory_rate",
    "success": true
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306200,
  "timestamp_utc": 1714284000
}
```

### Clinical observation (assessment submitted by CHW)

The SDK fires this when the SPICE app calls `onAssessmentSubmitted` (per `W12_SDK_INTEGRATION.md`). **Use `event_type: "spice_action_observed"`** — already in `CoachingEventType`. Put the SPICE workflow specifics in `payload_json` with `kind` as the discriminator. The same shape covers future referral / screening / vital-threshold variants without enum changes.

```json
{
  "id": "cd5a8b4c-6f0c-8d1e-cf9a-5c7d3a6b8f0e",
  "event_schema_version": 1,
  "session_id": "sess-550e8400-e29b",
  "patient_id_hash": "<sha256 of SPICE patientId>",
  "patient_visit_id": "<encounter UUID>",
  "village_id": "879e0e",
  "upazila_id": "893j",
  "event_family": "clinical_observed",
  "event_type": "spice_action_observed",
  "trigger_type": "workflow_event",
  "network_state": "online",
  "payload_json": {
    "kind": "assessment_submitted",
    "behavioural_gap_id": "...",
    "rule_engine_recommendation": {
      "is_referred": true,
      "destination_tier": "FACILITY_TYPE_UPAZILA",
      "referral_reasons": ["HIGH_BP"]
    },
    "chw_choice": {
      "referred_site_id": "...",
      "referred_clinician_id": "...",
      "referred_reason": "..."
    }
  },
  "event_date": "2026-04-28",
  "timestamp_local": 1714306400,
  "timestamp_utc": 1714284200
}
```

`outcome` stays **null**. The backend computes correctness post-hoc by pairing this event with the most recent `module_delivered` on the same `(chw_id, patient_id_hash, time-window)`. The compliance worker that does this pairing is W-12.7 — see Open Issues.

---

## Architectural rules

### 1. SDK sends raw observations, backend computes verdicts

Compliance, "correct referral", "matched recommendation" — all backend-side. The SDK puts the raw inputs (rule-engine recommendation + CHW's choice) in `payload_json` and leaves `outcome` null on `clinical_observed` events.

**Why:** if the SDK pre-computes correctness, every threshold change ships a new SDK build, and we can't recompute compliance on historical events when the rule changes. (See `W12_SDK_INTEGRATION.md` line 400.)

### 2. PII never crosses the SDK boundary

`name`, `phoneNumber`, `nationalId`, `dateOfBirth`, free-text address — these MUST NOT appear on any event. `patientId` is hashed to `patient_id_hash` at the SPICE call site before crossing into the SDK. (See `W12_SDK_INTEGRATION.md` §"PII boundary, made explicit.")

### 3. Idempotency is the SDK's job *and* the backend's

SDK keeps a stable `id` UUID across retries. Backend dedups on `id` via Redis SET-NX (24h TTL). Sending the same `id` twice is safe — the duplicate lands in `ack.duplicates` and never hits ClickHouse a second time.

### 4. ClickHouse is for analytics; Postgres is operational truth

`process_module_event_task` writes `chw_module_completion` + `chw_behavioural_gap_state` (Postgres). It runs even if the ClickHouse insert failed and the row is in the retry queue. Don't infer module completion from ClickHouse.

---

## Geo hierarchy mapping

SPICE owns the location hierarchy. Their canonical model is generic; deployments rebrand the levels via UI labels.

| SPICE-generic (DB columns) | Bangladesh UI label | Our telemetry field |
|---|---|---|
| Region | Division | (not on telemetry) |
| District | District | (not on telemetry) |
| Chiefdom | Upazila | **`upazila_id`** |
| Village | Union | **`village_id`** |
| Sub-village | Village | (not on telemetry) |

**Important:** what Bangladesh calls a "village" is SPICE's `subvillage_id`, not `village_id`. Confirm with the SPICE Android team which leaf they're sending in `village_id` before pilot ingest, otherwise analytics will silently mix Union-level and Village-level rows.

`upazila_id` is a Bangladesh-flavoured field name. Internally it is the SPICE-generic `chiefdom_id`. We keep the BD label for now to avoid a contract break; will revisit if/when we onboard a non-BD deployment.

The SDK does not currently have access to `chiefdomId` — `CHWWorkContext` only carries `villageId` on patient summaries. Adding `chiefdomId` to the `CHWWorkContext` Kotlin data class (and to the relevant lifecycle-hook payloads) is the planned extension; tracked in W-12 clarification asks (C2, C3).

---

## Open issues

The compliance worker is the only contract-adjacent gap not yet closed.

### Compliance worker (W-12.7) — the platform's core impact metric

The compliance worker is what joins **what we coached** (a `module_delivered` event) with **what the CHW did with a real patient** (a `spice_action_observed` event with `kind: "assessment_submitted"`) to answer the question "did the coaching change behaviour?"

```
event 1: module_delivered
         (we pushed CHW Ayesha module M about emergency referrals at 9:00)
event 2: spice_action_observed (kind=assessment_submitted)
         (Ayesha did a patient assessment at 11:00; rule-engine said
          "emergency referral", she chose "non-emergency")
                       ↓
worker joins on (chw_id, patient_id_hash, time-window)
                       ↓
computes: chw_choice == rule_engine_recommendation?
                       ↓
emits: coaching_outcome_correlation row
       { module_id, chw_id, encounter_id, complied=False, ... }
```

Without this worker, the dashboard can show "modules delivered: N" and "assessments made: M" but cannot show "modules that changed behaviour: K" — which is the core impact metric of the whole platform.

**Three blockers before this can be built:**

1. **Schema decision.** Does compliance live in a new `coaching_outcome_correlation` table (per the data design v1.1 spec), or computed on the fly in dashboard queries? Storage cost vs query cost — locks in different schemas.
2. **Pairing window.** How long after `module_delivered` does an assessment count as "this module's compliance"? Same day, same encounter, 24 hours? Depends on how the SDK groups module pushes.
3. **Rule-engine recommendation shape.** To compare "what we coached" vs "what the CHW did," we need the SPICE rule engine's recommendation in the assessment payload (`payload_json.rule_engine_recommendation`). The SDK side hasn't confirmed what that field will look like — string label, enum, or structured object.

This worker is W-12.7 in the SDK integration plan; not built here.

---

## Cross-references

- **`docs/W12_SDK_INTEGRATION.md`** — SDK-side architecture, lifecycle hooks, PII boundary, the three data streams. Line 400 has the compliance flow this doc operationalises.
- **`docs/W10_REVIEW_NOTES.md`** — the review notes for the v3.3 module-pipeline path (parallel to v3.0 scenario path). Background on why both paths coexist.
- **`docs/ARCHITECTURE_RESET.md`** — the broader architecture model (auto-publish, dashboard-first review, three runtime flows for module selection).
- **`packages/contracts/src/mc_contracts/telemetry.py`** — the Pydantic source of truth.
- **`packages/contracts/src/mc_contracts/enums.py`** — all enum values.
- **`infra/clickhouse/init.sql`** — `coaching_events` and `digital_events` table DDL.
- **`services/platform/src/platform_service/api/telemetry.py`** — ingest endpoint.
- **`services/platform/src/platform_service/celery_tasks.py`** — `process_module_event_task`, `process_gap_update_task`.
