# W-11 Review Notes — Card Validator Extension

**Audience:** sr dev who built the v3.0 `CardValidator`.
**Goal:** make this PR readable as "what changed and why."

Same parallel-path pattern as W-10. The v3.3 module pipeline produces a
different card shape (`ModuleCard` with `body_bn` + practice fields) than
the v3.0 `CoachingCardResponse`. We add a parallel `ModuleCardValidator`
that **reuses your forbidden-pattern regexes** and follows the same
hard/soft violation pattern, but applies v3.3-specific rules.

---

## TL;DR

| Touch | File | Change |
|---|---|---|
| Add | `infra/alembic/versions/0006_v33_module_pass_threshold.py` | Adds `module.pass_threshold_override Float NULL` |
| Extend | `db/models/module.py` | One column added to the SQLAlchemy model |
| Add | `services/module_card_validator.py` | New validator for `ModuleCard` + `ModuleQuizQuestion` |
| Extend | `workers/stage_d_draft.py` | Validates each card/question after critique loop; drops hard-violators, annotates soft warnings on `field_flags_jsonb` |
| Extend | `workers/module_completion_worker.py` | Uses `module.pass_threshold_override` if set, else falls back to `settings.quiz_pass_threshold_default` |
| Extend | `api/admin_reviewer.py` | `PATCH /admin/modules/.../cards/...` and `.../quiz/...` now re-validate the edited row and return `validator: {is_valid, hard_violations, soft_warnings}` in the response (non-blocking — reviewer is final authority) |
| Untouched | `services/card_validator.py` (v3.0) | Stays as-is. We import its 3 regex constants. |

**+23 tests, 643 passing total.**

---

## Reuse from your v3.0 CardValidator

We `from platform_service.services.card_validator import _DOSAGE_RE, _DRUG_RE, _DIAGNOSIS_RE`. Same forbidden expressions, same Bangladesh-specific drug list (artemether, lumefantrine, oxytocin, magnesium sulfate, etc.) — applied to a different card shape.

Pattern reuse: hard/soft violation distinction (hard → block, soft → warn). Same `Settings.spice_referral_set` reference data.

What's NEW for v3.3:
- **Bangla bleed detection** — `_bn` field with >25% Latin alphabet chars → likely the LLM forgot to translate. Soft warning.
- **Threshold consistency** — body number close to (off by 1–15) but not equal to a value in `thresholds_jsonb` → likely LLM drift. Soft warning. The narrow band avoids false positives where 90 (diastolic) coexists with threshold 140 (systolic).
- **Body length cap** — `body_bn ≤ 1200 chars`, `title_bn ≤ 120`, `next_action_bn ≤ 400`. Soft warnings, layout hints not safety.
- **Quiz options pairwise-distinct** — case-insensitive, whitespace-tolerant. Hard violation: dup options make the right answer obvious.
- **Quiz: correct_indices in range, not empty** — defensive even though Stage D validates this.
- **content_update card shape** — body_bn empty is OK iff `previous_practice_bn + current_practice_bn + rationale_for_change_bn` are all populated (the W-5 design uses practice fields as the content for content_update modules).

Per-module pass-threshold: was deferred from W-10. Now lives on `Module.pass_threshold_override` (Float NULL); the W-10 worker checks this first.

---

## Where the validator runs

### 1. Stage D (`workers/stage_d_draft.py`), step 6.5
After CardDrafter + QuizDrafter + critique loop, before persistence. Hard
violations DROP the card/question. If the card count drops below
`settings.card_min_count` (3), the candidate is failed with reason
`validator_dropped_too_many_cards` — same as if Stage D's existing
"insufficient" filter had caught it.

Soft warnings are written onto each card's `field_flags_jsonb`:
```json
{"validator_warnings": ["body_bn too long (1450 > 1200)", "..."]}
```
The reviewer surface (W-6) already echoes `field_flags_jsonb` back in the
draft-module view; reviewers see the warnings inline.

### 2. Reviewer edit (`api/admin_reviewer.py`)
On `PATCH /admin/modules/{candidate_id}/cards/{card_family_id}` and the
analogous quiz endpoint, we re-validate the edited row and return
`validator: {is_valid, hard_violations, soft_warnings}` in the response.
**Non-blocking** — even hard violations don't reject the edit; the reviewer
gets the warnings and decides. They're the final authority on edits.

### 3. Module completion (`workers/module_completion_worker.py`)
On every `MODULE_QUIZ_ATTEMPTED`, the pass/fail decision uses
`module.pass_threshold_override` if set, else `settings.quiz_pass_threshold_default`
(0.70). One-line change.

---

## Tunables

Hardcoded in `module_card_validator.py` for pilot; promote to settings if
reviewers want them per-tenant or want to tune from ops:

```python
_BANGLA_BLEED_LATIN_RATIO = 0.25
_BANGLA_BLEED_MIN_LENGTH = 30
_MAX_BODY_BN_CHARS = 1200
_MAX_TITLE_BN_CHARS = 120
_MAX_NEXT_ACTION_BN_CHARS = 400
_THRESHOLD_DRIFT_MIN = 1   # off by less = matches
_THRESHOLD_DRIFT_MAX = 15  # off by more = different concept
```

The drift band is the most likely tunable — may need narrowing or widening
once we see real reviewer feedback.

---

## Operations checklist

1. **Postgres migration**: `alembic upgrade head` will apply `0006_v33_module_pass_threshold` (additive, nullable column — safe for live tables).
2. **Field-flags consumer**: the SDK already receives `field_flags_jsonb` via `/sync/v3/modules` (added in W-9). No new contract — reviewer surface already shows it.
3. **No new env vars or external dependencies.**

---

## Tests (23 new)

```
tests/services/test_module_card_validator.py — 23 tests, pure-unit
```

Coverage:
- Happy path: clean card / clean quiz pass with no warnings
- Each hard rule: empty body, dosage, drug, diagnosis, empty options, dup options, bad correct_indices, empty option string
- Each soft rule: Bangla bleed, length caps, threshold near-miss, threshold equal-no-warning, threshold different-OOM-no-warning
- content_update shape: passes with practice fields and empty body_bn
- Bulk + flag annotation helpers

---

## Test-feedback bugs caught (that I fixed before flagging here)

1. **Threshold drift band was 0.5–2.0× ratio** → flagged 90 (diastolic) as drift from 140 (systolic). Narrowed to absolute drift of 1–15.
2. **Referral check fell back to `next_action_bn`** → every card mentioning "refer" got flagged. Restricted to explicit `referral_destination` field only.
3. **`body_bn empty` was hard-violation** → broke W-5 content_update tests where practice fields hold the content. Now passes if all three practice fields are populated.

---

## What's NOT in this PR

- **Per-tenant validator config**. Tunables are module-level constants. Promote to `Settings` if/when reviewers ask.
- **Validator status enum on each card** (the v3.0 validator stamps `validator_status="pass"|"warn"|"fallback"` on `CoachingCardResponse`). Our v3.3 cards just carry `field_flags_jsonb["validator_warnings"]`. The sync API exposes this directly so the SDK can render warnings if desired.
- **Auto-fix / sanitisation**. The v3.0 validator truncates oversized fields. The v3.3 path emits a soft warning and lets the reviewer decide whether to edit — keeps the LLM's output verbatim until a human acts.
- **Reusable `validator-only` admin endpoint** to re-validate a whole queue (e.g. after threshold tuning). Easy to add when needed.
