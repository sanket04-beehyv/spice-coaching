# Language deployment guide

The platform is **language-agnostic at the schema level**: content is stored as locale-keyed maps (`{"bn": "..."}`) rather than hardcoded `*_bn`/`*_en` columns. Each deployment instance configures a single CHW-facing primary locale.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEPLOYMENT_PRIMARY_LOCALE` | `bn` | CHW-facing locale (ISO 639-1 short code) |
| `DEPLOYMENT_REGION_CONTEXT` | `rural Bangladesh` | Geographic/persona context injected into LLM prompts |

Example Hindi deployment:

```bash
DEPLOYMENT_PRIMARY_LOCALE=hi
DEPLOYMENT_REGION_CONTEXT=rural India
```

## Content model

### Relational fields

| Table | Field | Shape |
|-------|-------|-------|
| `module` | `title_localized`, `description_localized` | `jsonb` locale map |
| `module_quiz_question` | `question_localized`, `case_setup_localized`, `options_localized`, `explanation_localized` | `jsonb` |
| `chat_frequent_question` | `question_localized` | `jsonb` |
| `module_candidate_draft` | `description_localized` | `jsonb` |

### Cards (`module_json.cards[]`)

Each translatable card field is a locale map with the deployment primary key:

```json
{
  "title": {"hi": "..."},
  "body": {"hi": "..."}
}
```

Search metadata uses the same locale-map pattern for list fields (see below).

### Module search metadata (`module.search_metadata_jsonb`)

Written by the post-publish search metadata worker after card drafting.

| Field | Shape | Notes |
|-------|-------|-------|
| `keywords` | `{"<primary>": ["...", ...]}` | CHW-facing clinical vocabulary |
| `search_phrases` | `{"<primary>": ["...", ...]}` | Natural-language search scenarios |
| `topic_tags` | `{"<primary>": ["...", ...]}` | Broad topical labels (snake_case) in primary language |
| `synonyms` | `{"<primary>": {"ABBREV": "expanded form"}}` | Abbreviation expansions in primary language (abbrev keys may be Latin) |
| `clinical_conditions` | `{"<primary>": ["...", ...]}` | Conditions or syndromes in primary language |
| `audience` | string | Always `chw_field_worker` |
| `rationale` | string | Reviewer-facing note (English) |
| `schema_version` | int | Schema version |

### Per-card search metadata (`module_json.cards[i].search_metadata`)

Written by the post-publish card search metadata worker.

| Field | Shape | Notes |
|-------|-------|-------|
| `retrieval_hints` | `{"<primary>": ["...", ...]}` | Search scenarios scoped to the card |
| `keywords` | `{"<primary>": ["...", ...]}` | Card-scoped clinical vocabulary |
| `questions` | `{"<primary>": ["...", ...]}` | FAQ-style questions the card answers |
| `synonyms` | `{"<primary>": {"ABBREV": "expanded form"}}` | Abbreviation expansions in primary language |
| `schema_version` | int | Schema version |

Clients resolve list fields with `metadata.keywords[locales.primary]` and synonym maps with `metadata.synonyms[locales.primary]` (same rule as card `title`/`body`).

Legacy `synonyms_en` flat dicts are migrated on read to `synonyms: {"en": {...}}`; new writes use `synonyms` with the deployment primary key only.

### Canonical rule

The **deployment primary locale** is required for all CHW-facing content.

## Device sync contract

`GET /sync/config` returns:

```json
{
  "thresholds": { "...": "..." },
  "locales": {
    "primary": "hi",
    "supported": ["hi"]
  },
  "server_time_utc": "..."
}
```

`GET /sync/modules` and related payloads use `title`, `description`, `question`, `options`, etc. as locale maps. Clients resolve display text with `content[locales.primary]`.

## Database migration

Migration `0030_localized_content` converted existing Bangla/English data to `{"bn": ..., "en": ...}` keys and transformed card JSON in place. Legacy `en` keys may remain in stored JSON but are ignored on read; new content is written with the primary locale key only.

After deploy, optionally re-run:

```bash
uv run python bin/backfill_localized_content.py
```

Re-generate embeddings after migration:

```bash
uv run python bin/regenerate_module_embeddings.py
```

### Switching deployment primary locale

When `DEPLOYMENT_PRIMARY_LOCALE` changes (e.g. `bn` → `hi`), existing stored
metadata maps retain the old locale keys only. Re-generate CHW-facing metadata
and embeddings for published modules:

1. Run the backfill script (cleans legacy `*_bn`/`*_en` suffix shapes if any remain):

   ```bash
   uv run python bin/backfill_localized_content.py
   ```

2. Re-run post-publish workers for each published module (card search metadata,
   then module search metadata) so `keywords`, `search_phrases`, `topic_tags`,
   `synonyms`, `clinical_conditions`, `retrieval_hints`, and `questions` are
   written under the new primary key.

3. Regenerate embeddings so BM25/vector indexes include the new locale text:

   ```bash
   uv run python bin/regenerate_module_embeddings.py
   ```

Legacy plain-list `topic_tags` values (pre-localization) are coerced to
`{"<primary>": [...]}` on read and during normalization.

## Android SDK migration

Replace direct field access:

| Before | After |
|--------|-------|
| `module.title_bn` | `module.title[config.locales.primary]` |
| `card.body_bn` | `card.body[config.locales.primary]` |
| `quiz.question_bn` | `quiz.question[config.locales.primary]` |

Fetch `locales` from `GET /sync/config` at startup and cache for offline use.

## Supported locale registry

Locale metadata (script ranges, symbol verbalization examples, annexure terms) lives in `packages/foundation/src/mc_foundation/locale.py`. Extraction quality heuristics reuse the same script ranges.

To add a new deployment locale, register it in `LOCALE_REGISTRY` before go-live.

## Review workflow

Module review attestation uses `primary_language_content` (replacing `bangla_content`). Validators check primary-locale script integrity instead of Bangla-specific bleed detection.

## Out of scope

- Telemetry geo fields (`upazila_id`, etc.) remain Bangladesh-labelled until a non-BD deployment needs renaming.
- Font/TTS rendering is client-side; the server supplies speakability rules via symbol verbalization prompts.
