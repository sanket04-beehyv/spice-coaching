# MicroCoaching Backend

MicroCoaching is implemented as a stateful platform service plus a private AI runtime inside one monorepo.

This document is the canonical reference for:
- the backend architecture
- the public and internal endpoint contract
- the current implementation status

Old drifted route names are not part of the contract and should not be used.

## Quick Start Docs

- Docs index and reading order: `docs/README.md`
- Setup issues and fixes: `docs/SETUP_TROUBLESHOOTING.md`
- Codebase map and ownership: `docs/PROJECT_NAVIGATION.md`

## Architecture

### Deployables

`platform-api`
- Public API for the Android SDK, admin flows, telemetry ingest, sync, and dashboards
- Owns PostgreSQL writes, ClickHouse writes, orchestration, and final response validation

`platform-worker`
- Async worker process from the same `platform_service` codebase
- Owns ingestion jobs, quiz jobs, and queued gap-profile updates

`ai-runtime`
- Private internal inference service
- Owns model-provider execution, embedding generation, and runtime response parsing
- Does not own PostgreSQL, ClickHouse, or public product workflows

### Shared Packages

`packages/contracts`
- Pydantic contracts, enums, DTOs

`packages/foundation`
- Shared config, logging, tracing, lightweight infra helpers, and the vendor-agnostic `VectorStore` protocol

### Ownership Rules

Platform owns:
- PostgreSQL schema and repositories
- Alembic migrations
- telemetry ingest and analytics writes
- sync contracts
- final coaching-card validation
- admin and dashboard APIs

AI runtime owns:
- provider adapters
- raw generation execution
- embedding generation
- runtime parsing

Shared packages do not own:
- SQLAlchemy models
- repositories
- business workflows
- domain services

## Repo Layout

```text
coaching-platform/
├── pyproject.toml
├── README.md
├── docker-compose.yml
├── infra/
│   ├── alembic/
│   └── alembic.ini
├── packages/
│   ├── contracts/
│   └── foundation/
└── services/
    ├── platform/
    │   └── src/platform_service/
    └── ai-runtime/
        └── src/ai_runtime/
```

## Canonical Endpoint Contract

### API root

Platform API routes are served under **`/medtronics-api`** (configurable via `API_ROOT_PATH`). Paths below are relative to that root; the full URL is `{api_root_path}` + path (e.g. `GET /medtronics-api/ready`, `POST /medtronics-api/coaching/counselling`).

### Authentication

When `SPICE_AUTH_ENABLED=true`, platform-api validates every request (except paths listed in `SPICE_AUTH_EXEMPT_PATHS`, default `ready`) by calling SPICE auth-service `POST {SPICE_AUTH_BASE_URL}/authenticate` with the caller's headers:

- `Authorization: Bearer <jwt>` (required)
- `client` (optional; defaults to `SPICE_AUTH_DEFAULT_CLIENT`, typically `mob` for the Android app)
- `auth-cookie` (optional; web clients)

Set `SPICE_AUTH_BASE_URL` to the auth-service root as seen by platform — either direct (`http://authservice:8089`) or via the nginx reverse proxy (`http://gateway/auth-service`).

Callers must obtain JWTs with the correct SPICE login client: admin web uses `client: web` (roles with `suiteAccessName: admin`); the Android SDK uses `client: mob` (roles with `suiteAccessName: mob`). Platform forwards the `client` header to `/authenticate` but authorization is based on roles embedded in the token.

### Authorization (two planes)

When SPICE auth is enabled, routes are split into **admin** and **device** planes (prefixes configurable via `SPICE_ADMIN_PATH_PREFIXES` and `SPICE_DEVICE_PATH_PREFIXES`):

| Plane | Path prefixes (under `API_ROOT_PATH`) | Who may call |
|-------|----------------------------------------|--------------|
| Admin | `admin`, `dashboard` | `isSuperUser`, or any role with `suiteAccessName == admin` |
| Device | `telemetry`, `sync`, `morning`, `coaching` | `isSuperUser`, `isJobUser`, or any role with `suiteAccessName == mob` |

Access is **strictly partitioned**: an admin principal cannot call device routes and vice versa, except `SUPER_USER` which may use both. Violations return `403` with `insufficient role for this API`.

Admin routes (including ingest, `POST /admin/ingest`) require **all** of: a valid SPICE token and admin-plane authorization when SPICE auth is enabled.

Default local/docker behavior keeps `SPICE_AUTH_ENABLED=false` so existing smoke tests work without a running auth-service.

### Error responses (RFC 7807)

Every HTTP error from **platform** and **ai-runtime** returns `Content-Type: application/problem+json` with:

| Field | Meaning |
|-------|---------|
| `type` | Relative catalog pointer `docs/error-codes.json#{code}` |
| `title` | Short title derived from the code |
| `status` | HTTP status |
| `detail` | Technical/debug message (not user-facing copy) |
| `instance` | Request path |
| `code` | Stable machine code — **clients map this to UX strings** |

Client catalogue (descriptions, typical status, domain): [`docs/error-codes.json`](docs/error-codes.json). Server enum: `mc_contracts.errors.ErrorCode`. When adding, removing, or renaming a code, update both in the same PR — pre-commit enforces parity. Validation failures (`422`) include `errors[]` (field locations). Failed ingest steps also persist `error_code` / `error_message` on `ingestion_run_step`, exposed on batch poll nodes.

### Platform API

v3.3 is **module-centric** (not scenario-centric). Device sync uses `/sync/*`; admin content management uses `/admin/modules/*` and `/admin/ingest`. Legacy scenario/counselling route names below are **not implemented** on platform-api — they remain in this doc only as historical context for the ai-runtime `GenerationType` enum.

#### Device-facing

`POST /coaching/rag-query`
- Embeds the question, runs vector similarity over published modules via the configured `VectorStore` backend (default: pgvector on `module.embedding`), builds context from module cards, calls `ai-runtime` for a grounded JSON answer, and returns `source_document` rows with optional object-storage presigned URLs for PDF/source attribution
- Response includes `suggested_questions`: follow-up questions (in `response_language`) grounded in the retrieved module content

`POST /telemetry/events`
- Accepts telemetry batches from the SDK
- Writes analytics rows to ClickHouse
- Queues module-completion and gap-update jobs to Redis for background processing

`GET /sync/modules?since=<ISO-8601>&user_id=<int>`
- Returns published modules (with quiz payloads) updated after `since`
- When `user_id` is provided, also returns `assigned_module_ids` for that user (individual, po_sk, geographical, and group rules); when omitted or when the user has no assignments, `assigned_module_ids` is empty
- When `user_id` is provided, also returns `requested_modules` — the CHW's full training-request history (`module_id` and/or free-text `requested_module_name`, optional `reason`, `submitted_at`); when omitted, `requested_modules` is empty

`GET /sync/triggers?since=<ISO-8601>`
- Returns trigger definitions updated after `since`

`GET /sync/gaps?chw_id=<int>&since=<ISO-8601>`
- Returns behavioural gaps, CHW gap states, module completions, and partial quiz progress for offline device use

`GET /sync/chat-faqs?since=<ISO-8601>`
- Returns 5–6 ranked FAQ suggestion chips as locale-keyed maps (`question: {"bn": "...", "en": "..."}`) synthesized nightly from clustered `digital_help_used` telemetry for the resolved tenant

`GET /sync/config`
- Returns current config thresholds and deployment `locales` (`primary`, `supported`) for device sync

`POST /sync/source-documents/presigned-urls`
- Batch presigned GET URLs for source documents (max 50 IDs per request)

`POST /sync/source-documents/presigned-thumbnails`
- Batch presigned GET URLs for source document thumbnails (max 50 IDs per request)

`GET /sync/source-documents/published`
- Return presigned GET URLs (and thumbnail URLs) for source documents where `sync_published_visible=true` and `status` is not `retired` (optional `domain` filters by `content_domain`; `limit`/`offset` paginate documents). Module payloads remain on `GET /sync/modules`.

`GET /sync/assigned-videos?user_id=<int>&limit=<int>&offset=<int>`
- Returns videos (`source_type=video`) assigned to the user via the same individual / `po_sk` / geographical / group rules as module assignment
- Each row includes `video_id`, `title`, `description`, thumbnail path + optional presigned URL, nullable `duration_ms` (from `MAX(source_page.end_ms)` when available), `assigned_at`, and nullable `video_progress` (`last_position_ms`, `percent_watched`, `completed`, `last_watched_at`)
- Paginated envelope: `{ videos, total_videos, total_pages, limit, offset, server_time_utc }` (default `limit=50`, max 200)
- When SPICE auth is enabled, device principals may only request their own `user_id`; empty assignments return `videos: []` with HTTP 200
- Watch progress is written offline via `POST /telemetry/events` with `event_type=video_progress_updated` (see `docs/TELEMETRY_CONTRACT.md`); there is no sync write endpoint

`POST /sync/modules/presigned-thumbnails`
- Batch presigned GET URLs for module thumbnails (max 50 IDs per request)

`GET /morning/cards?chw_id=<int>`
- Returns prioritized module IDs for morning review
- Uses the configured `morning_cards_max` threshold

Module training requests (CHW self-service access) are accepted via `POST /telemetry/events` with `event_type=module_requested` (top-level `module_id` and/or `payload_json.requested_module_name`, optional `payload_json.reason`). See `docs/TELEMETRY_CONTRACT.md`.

#### Admin-facing

`POST /admin/ingest/upload`
- Uploads one or more source files (multipart field `files`, max 10) to object storage and creates `source_document` rows with `status='uploaded'` (pipeline not queued; always `sync_published_visible=false`)
- Form fields: optional `titles` (JSON array, one title per file in order; if omitted, each title is the file’s basename stem), optional `descriptions` (JSON array of strings or nulls, one per file; omit for null descriptions), optional `override_duplicates` (JSON array of booleans, one per file — when `true`, re-upload even if the file’s `content_sha256` matches an already-`uploaded` or already-`ingested` `source_document`), optional `content_domains` (JSON array of content domains, one per file — `clinical` | `digital` | `clinical_with_app_workflows`; null/empty entries and omission default to `clinical`)
- Duplicate detection uses SHA256 of file bytes against `source_document` rows with `status='uploaded'` or `status='ingested'` (`failed` / `ingesting` do not block)
- Returns `201` with `status: uploaded`, `sources[]` (each with `source_document_id`, `title`, `source_type`, `stored_path`, `content_domain`, `status`), and optionally `skipped_duplicates[]` when some files were blocked; returns `409` Problem Details with `code=duplicate_content` when every file is blocked

`POST /admin/knowledge/upload`
- Uploads one PDF (multipart field `file`) and creates `source_document` row(s) with `status='uploaded'` and `sync_published_visible=true` (does **not** enqueue the ingest pipeline)
- Whole-file mode (omit `splits` or send `[]`): optional Form `title` (defaults to PDF basename stem), optional `thumbnail_storage_path` from a prior `POST /admin/files` upload
- Split mode: Form `splits` JSON array of `{start_page, end_page, title, thumbnail_storage_path?}` (1-based inclusive page ranges); each split becomes its own `source_document` with its own `source_document_family_id` and a physically cut PDF object under `source-documents/knowledge/`; the original full PDF is discarded after splitting; top-level `title`/`thumbnail_storage_path` are ignored
- Thumbnail paths (when provided) must already exist in object storage under an allowed prefix; missing/invalid paths return `400`
- Returns `201` with `status: uploaded` and `sources[]` (`source_document_id`, `title`, `source_type`, `stored_path`, optional `thumbnail_storage_path`, optional `start_page`/`end_page`, `status`)

`DELETE /admin/knowledge/{source_document_id}`
- Soft-deletes a knowledge source document by setting `status='retired'` (object-storage bytes are kept; cleanup is out of band)
- Only documents with `sync_published_visible=true` may be retired; ingest docs (`sync_published_visible=false`) return `403` with `code=forbidden`
- Missing id returns `404` with `code=source_not_found`; already-retired knowledge docs return `204` (idempotent)
- Retired documents are excluded from `GET /sync/source-documents/published` even while `sync_published_visible` remains `true`
- Returns `204` No Content

`POST /admin/ingest`
- Queues the v3.3 pipeline (A→B→C→D) per staged source on `platform-celery-worker`
- JSON body: `source_document_ids` (array, min 1 max 10), optional `override_duplicates` (booleans aligned to ids — when `true` on an already-`ingested` id, clones a new `source_document` row from stored bytes before queueing), optional `ingestion_instructions` (batch-wide steering text for Stage C module identification; sanitized at start and stored on `ingest_batch`), optional `cards_per_module` and `quizzes_per_module` (fixed card/quiz counts per module for this batch; stored on `ingest_batch`; must fall within deployment bounds), plus `assessment_mode` (stored on `ingest_batch`; `read_only` skips post-publish quiz generation); primary language is always the deployment primary locale; `content_domain` is set at upload and is not accepted here. Unknown fields (including removed `fuse_sources` and `skip_merge`) are rejected. Stage D always attempts published-module merge for normal ingest (cross-source fusion drafts skip merge internally). Cross-source fusion runs automatically after all pipelines finish when ≥2 sources are successfully queued; single-source batches skip fusion
- Accepts `source_document` rows in `uploaded` status; also `failed` (re-queue same row) and `ingested` only when `override_duplicates` is `true` for that id; already-`ingested` without override is treated like upload duplicates (`skipped_duplicates` on partial success, or `409` Problem Details with `code=duplicate_content` when every id is blocked); returns `422` with `code=source_not_uploaded` for other non-queueable states
- Returns `202` with `status: batch_queued`, top-level `batch_id` + `poll_url` (includes API root prefix, e.g. `/medtronics-api/admin/ingest/batches/{batch_id}`), `sources[]` (each with `source_document_id`, `run_id`, `title`, `source_type`, `stored_path`), and optionally `skipped_duplicates[]`
- Eagerly creates an `ingest_batch` (including assessment/cardinality/instructions config) and one `queued` `ingestion_run` per successfully queued source so the poll URL is valid immediately

`GET /admin/ingest/batches/{batch_id}`
- Polls tree-shaped progress for the whole ingest batch: per-source nodes (thumbnail → extract → module identify, with per-chunk identify nodes and candidates nested under each chunk via `source_chunk_ids[0]`, each candidate holding `card_draft` + post-publish stages) and optional top-level `fusion` when a multi-source batch ran fusion. Each node includes fixed-catalog `title`/`description`. Chunk children use `key: "chunk"` and `chunk_id` (e.g. `chunk-3`) with their own status/`error`/`error_code`/`error_message`; chunk and identify status roll up from children. Candidates with missing lineage or an unknown chunk id are omitted. Batch status rolls up to `queued` | `running` | `succeeded` | `failed` | `partially_succeeded`.
- When Stage D finds a similar active module, it writes a dual-path pair in the matched family without parking: **primary** (current-document cards) and **secondary** (LLM-merged cards), both `lifecycle_status=review_pending`, linked to each other and the matched tip. Both enqueue full post-publish; sibling candidates continue. The matched tip stays active until override. Pairs appear in the default module list; filter with `GET /admin/modules?status=review_pending` for review-pending only.
- Includes top-level `retry_url` when at least one stage is retryable (includes API root prefix, e.g. `/medtronics-api/admin/ingest/batches/{batch_id}/retry`); `null` when nothing is retryable. POST with no body; the server identifies every retryable failed stage and retries them.

`POST /admin/ingest/modules/{module_id}/override-merge`
- Promotes the secondary dual-path merge module for a **primary** `module_id` (`merge_secondary_module_id` set, status `review_pending`)
- Retires the primary and the matched source module; sets secondary to `draft` with `supersedes_module_id` pointing at the source. Secondary’s cards/quizzes/embeddings/search metadata are kept as-is (no copy).
- Returns `200` with `primary_module_id`, `secondary_module_id`, `source_module_id`, `secondary_lifecycle_status`; `400`/`404`/`409` Problem Details for invalid primary, missing modules, or non-`review_pending` state

`POST /admin/ingest/batches/{batch_id}/retry`
- Retries every retryable failed stage in the batch with no request body; identifies targets server-side and reuses the per-stage retry path for each. This is the URL returned as poll `retry_url`.
- Returns `202` with `results[]` (each with `run_id`, `stage`, `status` of `retry_queued` or `noop`, optional `candidate_id` / `chunk_id` / `reason`) and `poll_url` (includes API root prefix) when at least one retry was queued; `200` when every result is `noop` or there were no targets; `404` when the batch is missing

`POST /admin/files`
- Upload an admin file asset (object storage)

`GET /admin/files/presigned-url`
- Presigned GET for an admin file object

`POST /admin/modules` and `GET /admin/modules`
- Create and list modules. List supports optional `status` (`draft` | `published` | `retired` | `deactivated` | `review_pending`; default list excludes `retired` and `deactivated`, and includes `review_pending`), `limit` (default 50, max 200), `offset` (default 0), `sort_by` (`created_at` | `published_at` | `activated_at` | `last_deactivated_at` | `title` | `domain` | `lifecycle_status`; default `published_at`), and `sort_dir` (`asc` | `desc`; default `desc`). Returns a paginated envelope: `{ modules, total_modules, total_pages, limit, offset }`. Module summaries include optional dual-path merge FKs (`merge_secondary_module_id`, `merge_primary_module_id`, `merge_source_module_id`).

`GET /admin/modules/domains`
- Distinct `module.domain` values for admin filter dropdowns; optional `status` matches the modules list tabs

`GET /admin/modules/{module_id}`, `PUT /admin/modules/{module_id}`, `DELETE /admin/modules/{module_id}`
- Module CRUD. `PUT` requires `expected_version` (the version of the module row being edited). If that version is stale or another writer already created a newer family tip, returns `409` Problem Details with `code=module_version_conflict` (`expected_version`, `current_version`, `latest_module_id` as extensions); client must `GET` the latest module and retry. When the body is a **complete content snapshot** (`title`, `description`, `module_json`, `thumbnail_storage_path`, plus quiz as top-level `quiz` or nested `module_json.quiz`) and matches the tip, `PUT` is a no-op and returns the existing `id` / `version` (no new draft). `chatbot_faqs_only`, gap ids, and `editor_id` are ignored for equality. Omitted content fields still create a new version. `DELETE` retires the module (`lifecycle_status=retired`); when the module is a dual-path merge primary (`merge_secondary_module_id` set), the secondary is retired in the same operation. Retiring a secondary alone does not retire the primary. Response is `{ id, lifecycle_status, deprecated_at }` for the requested module only.

`GET /admin/modules/search`
- Search modules by title/content filters

`POST /admin/modules/{module_id}/regenerate-quiz` and `POST /admin/modules/{module_id}/regenerate-embedding`
- Enqueue post-publish quiz or embedding regeneration

`POST /admin/trigger-bindings` and related CRUD under `/admin/trigger-bindings/*`
- Manage trigger-to-module bindings

`GET /admin/ingestion-runs` and `GET /admin/ingestion-runs/{run_id}`
- List and inspect ingestion runs. List supports optional `status`, optional `q` (case-insensitive substring on `original_filename` or `title`), `limit` (default 50, max 200), `offset` (default 0), `sort_by` (`started_at` | `completed_at` | `status` | `document_label`; default `started_at`), and `sort_dir` (`asc` | `desc`; default `desc`). Returns a paginated envelope: `{ runs, total_runs, total_pages, limit, offset }`

`GET /admin/source-documents`
- List source documents for admin catalog views (ingest dropdowns, video upload table, knowledge catalog). Optional `status` (`uploaded` | `ingesting` | `ingested` | `failed` | `retired`; omit for all non-retired statuses; repeat and/or comma-separate for multiple; use `status=retired` to list retired only), optional `sync_published_visible` (`true` = knowledge docs, `false` = ingest docs; omit for both), optional `source_type` (`pdf` | `pptx` | `docx` | `audio` | `video`; repeat and/or comma-separate for multiple), optional `q` (case-insensitive substring on `original_filename` or `title`); supports `limit` (default 50, max 200), `offset` (default 0), `sort_by` (`ingested_at` | `title` | `source_type` | `status` | `content_domain` | `original_filename`; default `ingested_at`), and `sort_dir` (`asc` | `desc`; default `desc`). Returns a paginated envelope: `{ source_documents, total_source_documents, total_pages, limit, offset }`. Each row includes `stored_path` (object-storage path for download via existing presign), plus `description` and `thumbnail_storage_path` when set.

`PATCH /admin/source-documents/{source_document_id}`
- Update `title` and/or `description` without re-ingest. Title, when provided, must be non-empty.

`PUT /admin/source-documents/{source_document_id}/thumbnail`
- Replace the source document thumbnail (multipart image: PNG, JPEG, or WebP). Does not re-upload the source file or start ingestion.

`GET /admin/video-assignments`, `POST /admin/video-assignments`, `DELETE /admin/video-assignments/{assignment_id}`
- Assign uploaded videos (`source_document` with `source_type=video`) to individuals / PO+SK (`po_sk`) / geography / group, mirroring module assignment semantics. Assignment is independent of ingest status and does not trigger re-ingest. `GET` supports optional `source_document_id` and `assignment_type` filters for pre-populating the admin dialog.

`GET /admin/module-demand/summary`
- LLM narrative plus top-K form + chatbot (`digital_help_used`, keyed on `module_id`) demand, split into available (assign / open draft) and unavailable (create); K from config key `module_demand_top_k`. Served from a daily-precomputed snapshot (Celery beat `platform.refresh_module_demand_summary`), with a live-build fallback on cache miss

`GET /admin/module-demand/modules/{module_id}/requestors`
- Form requestors for a module (source `form` in V1), with `already_assigned` from individual assignments

`POST /admin/module-demand/modules/{module_id}/assign`
- Bulk individual assign from the demand flow (served from the assignments router); records `module_demand_assigned` attribution audit


#### Dashboard-facing

`GET /dashboard/supervisor/{chw_id}`

`GET /dashboard/district/{upazila_id}`

`GET /dashboard/llm-quality`

`GET /dashboard/digital-help-modules`

`GET /dashboard/digital-help-modules/{module_id}/questions`

`GET /dashboard/digital-help-modules/{module_id}/requests`

`GET /dashboard/module-creation-suggestions`

`GET /dashboard/module-creation-suggestions/{suggestion_id}`

`GET /dashboard/team-activity`

`GET /dashboard/team-activity/users/{user_id}/questions`

`GET /dashboard/document-usage`

Current state:
- these routes are part of the canonical API surface
- `GET /dashboard/supervisor/{chw_id}` reads from the ClickHouse `chw_daily_summary` materialized view (see `infra/clickhouse/init.sql`)
- `GET /dashboard/llm-quality` queries ClickHouse when `llm_daily_summary` exists in the deployment
- `GET /dashboard/digital-help-modules` ranks modules by combined `digital_help_used` + `module_requested` event volume over required `from_date`/`to_date` (UTC inclusive), keyed on concrete `module_id` (events without `module_id` ignored, including free-text requests; no family roll-up). Each item exposes `digital_help_count` and `module_requested_count`; response totals are `total_digital_help` and `total_module_requested`. Enriched with module titles from PostgreSQL; supports `limit` (default 20) and `offset` (default 0) pagination with `total_modules` in the response; `from_date > to_date` → 422
- `GET /dashboard/digital-help-modules/{module_id}/questions` uses the same auth/tenant model as the list (`resolve_tenant_id_for_dashboard`, optional `tenant_id`). Required `from_date`/`to_date`, `limit`/`offset` (default 50 / max 200). Returns deduplicated chatbot questions for that concrete `module_id` from `digital_help_used` (`payload_json.question`, fallback `query`): newest-first by latest occurrence, with `occurrence_count` and latest raw `question` text; blank question text omitted; `title` enriched from PostgreSQL when present (null if unknown). Empty window / unknown module → 200 with empty `questions`
- `GET /dashboard/digital-help-modules/{module_id}/requests` uses the same auth/tenant model. Required `from_date`/`to_date`. Returns a single aggregate `module_requested_count` for that concrete `module_id` only (free-text / `requested_module_name`-only events without `module_id` are not counted); includes `title` when present. No pagination
- `GET /dashboard/module-creation-suggestions` lists daily LLM suggestions for modules to create next, inferred from unattributed `digital_help_used` (no `module_id`) and free-text `module_requested` events. Required `from_date`/`to_date`, optional `limit`/`offset` (default 20). Same dashboard tenant auth. Items may be `matched_draft` (existing draft `module_id`) or `proposed_topic`. Populated by Celery beat `platform.refresh_module_creation_suggestions` (prior UTC day)
- `GET /dashboard/module-creation-suggestions/{suggestion_id}` returns one suggestion with its evidence: deduped chat `questions` and free-text `requests`. Wrong tenant / unknown id → 404
- `GET /dashboard/team-activity` is a **device-plane** exception under the `dashboard` prefix for authenticated team organizers (mobile SDK). Query: `from_date`, `to_date` (UTC inclusive), `limit`, `offset` (no `po_user_id`). With Spice auth enabled, organizer scope comes from the PO device JWT; admin/non-PO → 403. With auth disabled, there is no organizer filter — activity is reported for all SK users. Returns a team summary plus paginated member activity (assigned module completion, chatbot usage by module, and per-member `refreshers_generated` / `refreshers_completed`). ClickHouse reads use MVs `chw_daily_summary` and `chw_digital_help_daily` for activity/chatbot metrics; refresher counts are folded in-app from ordered `coaching_events` rows with `event_type = module_quiz_attempted` in the date window (incorrect opens a refresher per `quiz_id`, subsequent correct closes it; window-only pairing). Per-member `last_chat_at` and `last_active_at` are all-time latest activity dates (UTC midnight) from those MVs.
- `GET /dashboard/team-activity/users/{user_id}/questions` keeps device-plane auth with `po_user_id` for admin/auth-off (unlike the list route). Required `from_date`/`to_date`, `limit`/`offset` (default 50 / max 200). Returns deduplicated chatbot questions for one on-team member from `digital_help_used` (`payload_json.question`, fallback `query`): newest-first by latest occurrence, with `occurrence_count` and latest raw `question` text. Off-team `user_id` → 403. Blank question text is omitted. No change to the team-activity list payload.
- `GET /dashboard/document-usage` aggregates `document_viewed` telemetry via the `document_view_daily` MV and raw `coaching_events` into one response (KPIs, per-document rows, event drill-down). Shared filters: `from`/`to`, `upazila_id`, `district`, `po_id`, `sk_id`, `user_id`, `document_id`, optional `tenant_id`. Geography filters resolve via the org user map (`upazila_id` parity with `district`). PO/AM viewers are hierarchy-scoped; platform admins see all. The MV is defined in `infra/clickhouse/init.sql` (drop legacy `document_open_daily` if present on existing envs).
- Admin module demand (`GET /admin/module-demand/summary`) soft-fails chatbot CH merge so form demand still returns
- `GET /dashboard/district/{upazila_id}` still returns `501 Not Implemented` until implemented

#### Operational

`GET /ready`
- Readiness probe: PostgreSQL, Redis, ClickHouse, ai-runtime (including provider alignment), and object storage

### AI Runtime

#### Internal-only

`POST /internal/generate/{generation_type}`
- Canonical private generation endpoint
- Supported generation types are validated against the shared enum
- This intentionally consolidates counselling, IT help, extraction, and quiz generation into one internal route

`POST /internal/embed`
- Private embedding endpoint used by platform ingestion/retrieval flows

`GET /health`
- Basic runtime liveness endpoint

## Request Flows

### Coaching RAG

1. SDK calls `POST /coaching/rag-query`
2. Platform embeds the question via `POST /internal/embed` on `ai-runtime`
3. Platform retrieves similar published modules through `VectorStore.search` (pgvector adapter today)
4. Platform builds a grounded prompt and calls `POST /internal/generate/coaching_rag`
5. Platform returns the JSON answer with source-document attribution

### Telemetry

1. SDK calls `POST /telemetry/events`
2. Platform validates and translates telemetry rows
3. Platform writes accepted telemetry to ClickHouse
4. Platform queues module-completion and gap-update jobs to Redis
5. Worker consumes queued jobs and updates PostgreSQL state

### Sync

1. SDK calls `GET /sync/modules?since=...` for published modules and quizzes (optional `user_id` for `assigned_module_ids` and `requested_modules`)
2. SDK calls `GET /sync/triggers?since=...` and `GET /sync/gaps?chw_id=...&since=...` as needed
3. SDK calls `GET /sync/chat-faqs?since=...` for bilingual FAQ suggestion chips (clustered + LLM-synthesized nightly)
4. SDK calls `GET /sync/config` for threshold/config values
5. SDK uses presign endpoints for offline PDF/thumbnail access

## Current Implementation Status

### Implemented

- monorepo layout with `platform`, `ai-runtime`, `contracts`, and `foundation`
- single Alembic chain under `infra/alembic`
- v3.3 module-centric sync (`/sync/modules`, `/sync/triggers`, `/sync/gaps`, `/sync/chat-faqs`, presign batches)
- coaching RAG via platform → internal AI runtime
- telemetry ingest with ClickHouse writes
- Redis-backed Celery workers for:
  - v3.3 ingest pipeline (A→B→C→D) and cross-source fusion
  - post-publish quiz, embedding, and gap classification
  - module-completion telemetry processing
- morning-card selection with config-driven `morning_cards_max`
- SPICE auth middleware with admin/device authorization planes (when `SPICE_AUTH_ENABLED=true`)
- `/ready` readiness probe with dependency checks

### Intentionally Designed This Way

- `ai-runtime` uses one generic internal generation route:
  - `POST /internal/generate/{generation_type}`
- This is an intentional consolidation, not route drift

### Not Yet Fully Implemented

- `GET /dashboard/district/{upazila_id}` (returns `501`)
- legacy device routes: `POST /coaching/counselling`, `POST /coaching/quiz-answer`, `POST /coaching/it-help`
- legacy sync route: `GET /scenarios/sync?since_version=N` (replaced by `GET /sync/modules?since=...`)
- legacy admin scenario/document routes under `/admin/scenarios/*` and `/admin/documents/*`
- full config-management admin endpoints beyond threshold sync
- richer dashboard and analytics materialization flows

## Standards Decisions

- No public generic chat or unrestricted RAG endpoints are exposed
- The SDK talks only to `platform-api`
- `ai-runtime` is private and token-protected
- Platform is the system of record
- Legacy drifted route aliases are not part of the API contract
- The workspace is managed with `uv`, and `uv.lock` should be committed

## Local Development

### Prerequisites (first-run)

```bash
cp .env.example .env           # then fill GOOGLE_API_KEY
uv sync --locked --all-packages --group dev
```

Without `.env`, `docker compose` fails at parse time because `GOOGLE_API_KEY` is declared required. See `docs/SETUP_TROUBLESHOOTING.md` for other known failures.

### Pre-commit

After syncing dev dependencies, install git hooks once:

```bash
uv run pre-commit install
```

Hooks run ruff (lint + format), Pyright type checking (on Python changes), and basic file hygiene on each commit. To check the whole repo without committing:

```bash
uv run pre-commit run --all-files
```

### Environment

### Run services

Platform API:

```bash
uv run uvicorn platform_service.main:app --host 0.0.0.0 --port 8000
```

Platform worker:

```bash
uv run python -m platform_service.workers.main
```

AI runtime:

```bash
uv run uvicorn ai_runtime.main:app --host 0.0.0.0 --port 8001
```

### Run with Docker Compose

1. Create a root `.env` file (or export variables in your shell) and set:
   - `GOOGLE_API_KEY` (required for `ai-runtime`; `GEMINI_API_KEY` is accepted as fallback)
   - `AI_RUNTIME_TOKEN` (optional, defaults to `dev-internal-token`)
   - `APP_ENV` (optional, defaults to `development`)
2. Start the stack:

```bash
docker compose up --build
```

Expected compose behavior:
- `migrate` runs once and exits with code `0`
- `clickhouse-init` runs once and exits with code `0`
- `db`, `redis`, `clickhouse`, `ai-runtime`, and `platform-api` are running
- `platform-celery-worker` and `platform-celery-beat` are running

Quick verification:

```bash
docker compose ps
curl -fsS http://localhost:8000/medtronics-api/ready
curl -fsS http://localhost:8001/health
```

### Migrations

Run Alembic as a separate step:

```bash
uv run alembic -c infra/alembic.ini upgrade head
```

Migrations should not be auto-run by application startup.

## Next Recommended Work

1. Implement `GET /dashboard/district/{upazila_id}`.
2. Add rate limiting on device-plane routes.
3. Add sync pagination for large module catalogs.
4. Add remaining planned admin config-management flows.
5. Extend CI with type-checking beyond ruff (pyright/mypy) where practical.
