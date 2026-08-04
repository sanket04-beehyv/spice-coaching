# Architecture Reset

This doc is the single source of truth for the corrected architecture. It supersedes assumptions baked into v3.3 (`MicroCoaching_Architecture_v3.md`, `MicroCoaching_Content_Pipeline_v3.md`, `MicroCoaching_Data_Model_v3.md`) where they conflict.

This is **not** a pilot-scope cut and post-pilot expansion. It is the architecture we are building, period. Where v3.3 is wrong it is removed; where v3.3 is right it stays.

---

## Scope

### In scope (this repo)
- Ingestion pipeline: PDF → published modules
- Module storage and versioning
- Embedding generation for admin/web semantic search
- Admin/reviewer dashboard backend (CRUD on modules, quality flags, visibility windows, trigger-binding authoring)
- Pipeline run history surface

### Out of scope (Android repo / SPICE integration)
- Mobile app delivery, on-device caching, sync
- On-device or server-side LLM at runtime (card Q&A, SPICE rule humanisation)
- Telemetry collection (we own the schema for telemetry tables; we do not implement the producer)
- SPICE workflow event integration

---

## Conceptual model

### Cards have stable family IDs across module versions
- Cards are relational rows in `module_card`, keyed by `card_family_id` + `card_version` (same versioning pattern as `module_quiz_question`).
- `module.module_json` holds only module-level data (e.g. `attachments`); card content lives in `module_card`.
- Telemetry (`module_card_viewed`) and quiz linkage (`primary_card_family_id`) reference `card_family_id`.

### Module is the unit of meaning. Cards are slices of one module.
- A module is a coherent behavioural topic (pregnancy referral, postnatal care, etc.).
- Cards inside a module are presentation slices for screen rendering — paginated content.
- Cards belong to exactly one module version. Cross-module reuse is not a concept.

### No assignment.
- There is no CHW-to-module assignment table or workflow.
- Surfacing to CHWs is computed at fetch time by the runtime (Android repo) from telemetry, SPICE rules, and visibility windows.
- This repo's job ends at "module is published and findable."

### Auto-publish; dashboard is a correction surface, not a gate.
- Pipeline writes modules with `clinically_reviewed = false`.
- No reviewer claim queue. No publish gate. No `pending_review` state.
- The admin dashboard surfaces all published modules; reviewer can edit (creates new version), retire, set the `clinically_reviewed` flag, or set a visibility window.

### Versioning has two distinct triggers.
- **Routine reviewer correction** (typo fix, citation update): in-place edit, version bump for audit, **no** visibility window. Next fetch sees updated content.
- **Real content change** (policy update, methodology shift): version bump **plus** `visibility_window` set so the module surfaces in "what's new" for a defined period.

The pipeline never decides which is which. Only the reviewer/admin does, via dashboard action.

---

## Three runtime flows (Android repo, listed for context only)

| Flow | Trigger | Source of "which module" |
|---|---|---|
| Morning / between-visit refresher | CHW opens app | telemetry → `chw_behavioural_gap_state` → `module_trigger_binding` |
| In-workflow rule humanisation | SPICE rule fires (e.g. BP threshold breach) | rule event → workflow-trigger binding → module → on-device or server LLM grounds the sentence in module content |
| "What's new" campaign | New/corrected module published with active visibility window | `module.visibility_window` filtered by current time |

These are not implemented here. They are listed so the schema decisions in this repo support them.

---

## Pipeline shape

Three stages. No separate Stage B. No Stage F. No precondition gate. No gap context in prompts.

### Stage 1 — Extract
Single per-page LLM call. Returns text blocks with structural labels (`text`, `heading`, `list_item`). Outline is assembled deterministically from heading-labeled blocks; no separate stage, no regex/font heuristics.

Persists: `source_document`, `source_page`, `content_block`, and the assembled outline on `source_document.outline_jsonb`.

Stage failure conditions (all hard-fail; pipeline stops):
- LLM call errors after retries
- Outline ends up empty (no heading-labeled blocks). Currently this is silently recorded as success — see *Operational fixes*.

### Stage 2 — Identify + Draft
Two LLM calls per ingestion run (effectively):
1. Corpus-level LLM call: given outline + content blocks, propose N modules with their scope and cited blocks. Output goes into `module_candidate_draft` rows as ephemeral pipeline state.
2. Per-module LLM call: for each candidate, draft cards in the deployment primary locale (canonical) plus mirror locale when configured, plus card source provenance. Output writes directly to `module` rows as **`lifecycle_status='draft'`**. When a newly drafted candidate semantically matches an existing **active** module (any non-retired row; latest version per family), a second LLM call merges the old and new card sets (new content wins on conflict), **retires** the matched row, and creates a new draft version in the same `module_family` (reusing all `module_behavioural_gap` links). Quiz + embedding workers run on the merged draft.

Failure handling: if a candidate fails to draft, **skip it**. Log the failure to `ingestion_run_step.output_summary_jsonb` with the cited blocks. Auto-publish principle: only renderable modules ship. Half-modules to CHWs is worse than missing modules.

Gap context is **not** in the prompt. Gaps are runtime telemetry; the pipeline produces topic-modules, the reviewer maps modules to gaps via dashboard.

### Stage 3 — Publish
Writes `module.status = published`, `module.clinically_reviewed = false`. Enqueues post-publish jobs. No human gate.

### Post-publish jobs (separate Celery workers)
- **Embedding generation**: produce one vector per module from card text; write to `module.embedding`. Failure does not block the module — module is still readable, just not findable via semantic search until retry.
- **Quiz generation**: port from knowledge-layer's `app/services/quiz_generation.py`. Writes `module_quiz_question` rows. Failure does not block the module — quiz can be generated later or authored manually.

---

## Schema decisions

### Keep (no change)
- `source_document`, `source_page`, `content_block` (provenance layer; migration 0002)
- `module_quiz_question` (FK target for `chw_quiz_attempt`; migration 0003)
- `module_card` (relational card rows; FK from `module.id`; migration 0033)
- `behavioural_gap`, `module_trigger_binding`, `chw_behavioural_gap_state` (runtime tables; migration 0004)
- `chw_module_completion` (telemetry schema; migration 0005)
- `chw_quiz_attempt` (telemetry schema)
- `ingestion_run`, `ingestion_run_step` (pipeline tracking)

### Modify
- `module`:
  - **Keep** `module_json JSONB` for module-level attachments only (cards moved to `module_card`)
  - **Add** `embedding vector(N)` (per-module vector for admin/web semantic search; pgvector index)
  - **Add** `visibility_window TSTZRANGE` (nullable; when active, module surfaces in "what's new")
  - **Confirm** `clinically_reviewed BOOLEAN` exists (default false)
  - **Confirm** `status` supports the simplified set: `draft | published | retired` (no `pending_review`)
- `module_candidate_draft`:
  - **Drop** `claim`, `is_claimed`, `claimed_by`, `is_expired`, and any other reviewer-queue workflow fields
  - **Keep** the table as ephemeral pipeline state for Stage 2 retry semantics and run-history visibility

### Drop
- `module_card_membership` table (cross-module reuse is not a concept)
- `module_card_embedding` table (replaced by `module.embedding`)
- Any `chw_module_assignment`-like table if it exists (no assignment concept; verify in migrations and drop)
- `module_family` table if it exists and is unused (verify; the v3.3 design used it for grouping but we don't need it for the corrected model)

### Migration plan
A new migration `0007_architecture_reset.py` performs:
1. Drop the three tables above
2. Add `module.module_json`, `module.embedding`, `module.visibility_window`
3. Strip workflow fields from `module_candidate_draft`
4. Adjust `module.status` enum if needed

Down-migration restores the dropped columns/tables (best-effort; data loss expected since we're collapsing card rows into JSON).

---

## Code changes

### Delete entirely
- W-6 reviewer surface: `services/platform/src/platform_service/services/admin_reviewer.py`, `reviewer_service.py`, `module_publisher.py`, claim flow, queue/release endpoints. Verify the actual file paths and remove all of them.
- Stage F (review) code: any module_review_*.py, review_queue_*.py
- Legacy v3.0 code that's been superseded: `scenario_*` modules, `coaching_orchestrator`, `ingest_worker` (the pre-v3.3 worker). Confirm what's still referenced before deleting.
- `services/platform/src/platform_service/services/insufficient_source_filter.py` — the precondition gate. May be repurposed as a quality FLAG (write `quality_flags_jsonb` on the module for dashboard display) but never as a hard gate that prevents drafting.

### Add
- Stage 1 fused extractor: replaces `services/platform/src/platform_service/workers/extractors/llm_outline_extractor.py` and the per-page text extractor. One LLM call per page returning `{block_text, block_type, heading_label}` per block; outline assembled deterministically afterward.
- Post-publish embedding worker
- Post-publish quiz generation worker (port from knowledge-layer)
- Admin dashboard endpoints (replacing W-6's reviewer endpoints):
  - `GET /modules` — list with filters (`status`, `clinically_reviewed`, `has_visibility_window`, full-text query, embedding-similarity query)
  - `GET /modules/:id`
  - `PUT /modules/:id` — creates new version
  - `POST /modules/:id/clinically-reviewed` — flip the flag
  - `POST /modules/:id/visibility-window` — set/clear the window
  - `DELETE /modules/:id` — retire (soft-delete; status → retired)
  - `GET/POST/PUT/DELETE /trigger-bindings`
  - `GET /ingestion-runs`, `GET /ingestion-runs/:id` (includes failed Stage 2 proposals)

### Modify
- `services/platform/src/platform_service/workers/pipeline_orchestrator.py`:
  - Remove Stage B as separate stage
  - Remove Stage F (review) coordination
  - Stage 3 publish writes module + enqueues embedding + quiz jobs
  - Fix `MissingGreenlet` at line ~493 (failure path lazy-loads `step.id` outside greenlet context — wrap failure recording in a fresh async session)
- `services/platform/src/platform_service/services/card_drafter.py`:
  - Output writes to `module.module_json` directly, not to `module_card` rows
  - Refusal vocabulary stays (`no_actionable_content`, `single_concept_only`, etc.) but maps to "skip this candidate" not "blocked at gate"
- Stage C prompts (in `services/platform/src/platform_service/services/prompts/`): drop gap-list context entirely. No `behavioural_gap` references in the LLM prompt.

---

## Operational fixes (do these FIRST so we can validate as we refactor)

These are blockers we discovered while running the current branch end-to-end. Fix them before the refactor work because we need a working baseline to validate each refactor step against.

1. **Vertex env passthrough in `docker-compose.yml`** (`ai-runtime` service block).
   Add `google_use_vertex`, `google_cloud_project`, `google_cloud_location`, `GOOGLE_APPLICATION_CREDENTIALS` to the environment block, plus a credentials volume mount. Currently only `google_api_key` is forwarded, so Vertex deployments fail at startup with `Developer API mode requires a real api_key`.

2. **Stage B model selection.**
   `services/platform/src/platform_service/workers/extractors/llm_outline_extractor.py` calls `gemini-2.0-flash` and falls back to `gemini-2.0-pro`. Both return 404 on the `microcoaching` Vertex project. Generate models are owned by ai-runtime `generation_profiles` (default `gemini-2.5-flash`); platform must not select model ids. Will go away when Stage B is folded into Stage 1, but needs fixing now for the current code path to work.

3. **MissingGreenlet at `pipeline_orchestrator.py:~493`.**
   Failure-recording path crashes with `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called` because `step.id` lazy-loads outside greenlet context. Wrap the failure path in a fresh async session.

4. **Stage B "succeeded with empty outline" failing-as-success.**
   When the outline LLM returns nothing parseable, the stage records `outline_method: "failed", sections_count: 0` but marks the run step `succeeded`. Stage C then runs blind. Fail the stage if `sections_count == 0` (or fold this into the new Stage 1, which has a single success contract: text + outline both non-empty).

5. **Migrate image staleness.**
   `docker compose up` doesn't rebuild the migrate image. When migrations are added, `docker compose build migrate` must run before `docker compose run --rm migrate alembic upgrade head`. Document in `docs/SETUP_TROUBLESHOOTING.md`.

---

## Refactor order

Each step is self-contained, reviewable independently, and brings the codebase closer to the corrected architecture. After step 5, end-to-end ingestion against the BRAC SK manual / UHIS RMNCH PDF should work.

1. **Apply operational fixes** (above section, all five)
2. **Delete W-6 reviewer surface** (claim/queue/release endpoints, admin_reviewer.py, reviewer_service.py, module_publisher.py)
3. **Delete legacy v3.0 code** (scenario_*, coaching_orchestrator, pre-v3.3 ingest_worker)
4. **Schema migration `0007_architecture_reset`**: drop `module_card`, `module_card_membership`, `module_card_embedding`; add `module.module_json`, `module.embedding`, `module.visibility_window`; strip workflow fields from `module_candidate_draft`
5. **Fold Stage A + B into Stage 1** (single per-page LLM call producing structured blocks; outline assembled deterministically)
6. **Drop gap context from Stage C prompt** (no behavioural_gap references in the LLM input)
7. **Migrate Stage D output** to write directly to `module.module_json` instead of `module_card` rows
8. **Make Stage 3 auto-publish**: writes `module.status = published, clinically_reviewed = false`; remove candidate review queue logic
9. **Move quiz generation to a separate Celery task** (port `app/services/quiz_generation.py` from knowledge-layer; trigger on module publish)
10. **Move embedding generation to a separate Celery task** (per-module vector; trigger on module publish)
11. **Write new admin dashboard endpoints** (the list in *Code changes → Add*)

---

## Forks recorded

These were called explicitly:

- **`module_candidate_draft`**: kept as ephemeral pipeline state (no claim/expiry workflow). Used for per-candidate retry semantics and dashboard "drafting failed for these proposals" trail.
- **Stage 2 partial drafting failure**: skip failed candidates; log to `ingestion_run_step.output_summary_jsonb` with cited blocks. Reviewer who looks at run history sees what didn't draft and can author manually in the dashboard.
- **W-6 reviewer surface**: delete entirely. Dashboard endpoints written fresh against the corrected model (~5 endpoints vs ~11 in W-6).
- **Embedding at publish**: side-job, not in Stage 3 critical path. Module publishes immediately; embedding is asynchronous. If embedding fails, module is still readable in dashboard via title and full-text search.
- **Card stable IDs**: none. Module-version is the join unit for telemetry and diffs. When a module re-versions, the new version is opaque to the old.

---

## What v3.3 got right (and stays)

For the record, so we don't accidentally throw it out:

- Page-based extraction with section overlay via `heading_path_jsonb` on `content_block`. Section-only extraction would lose the deterministic page-citation provenance reviewers need.
- Deployment primary locale as canonical for CHW-facing content (mirror locale generated when configured, not vice versa). See `docs/LANGUAGE_DEPLOYMENT.md`.
- `mc_contracts` package as the typed boundary between platform and ai-runtime.
- ai-runtime as stateless service. No DB, no Redis, no ClickHouse.
- AIRuntimeClient as the only LLM-call path from platform.
- Alembic as a separate step, not at app startup.
- Python urllib healthcheck probe pattern.
- The provenance-first design: every card cites `content_block_id`s; no card text is generated without grounding.

---

## What v3.3 got wrong (and is being removed)

- Gap-driven Stage C: gaps are runtime telemetry, not source-document structure. Stage C was prompted to fit candidates to a seeded gap list; this produced narrow provenance, broken candidates, and threading of runtime concerns into ingestion.
- Reviewer-as-publish-gate: `pending_review` state, claim queue, expiry workflow, separate Stage F. Removing the human bottleneck is the entire point of the product; gating publish on a single Bengali clinician was the bottleneck.
- Module-as-collection-of-reusable-cards: encoded an LMS abstraction (cards as library objects assembled into modules) that contradicts how CHW manuals are organised (modules are coherent topics; cards only mean something inside their module).
- Per-card row breakdown: justified by per-card features (embedding, audit, edits) that are either wrong (per-card embedding when module-level retrieval suffices) or implied by the module-as-collection abstraction.
- Stage A and Stage B as separate stages: outline detection is a labelling task on the same input as extraction. Splitting them creates two failure surfaces and the silent-failure mode we observed where Stage B records "succeeded" with `outline_method: failed`.
- Insufficient-source filter as automatic gate: rejects candidates before Stage D ever runs based on tunable thresholds. With auto-publish + reviewer-as-correction, the human is the right gate, and threshold-tuning never converges.
- "Pilot scope vs post-pilot" framing in the v3.3 docs themselves: led to a design where many features were "in v3.3 but feature-flagged off." This is a smell; either build it or don't. The corrected architecture has no feature-flagged-off entities.
