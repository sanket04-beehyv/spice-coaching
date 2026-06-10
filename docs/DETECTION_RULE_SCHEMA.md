# Detection rule schema

`behavioural_gap.detection_rule_jsonb` holds on-device evaluation rules synced via
`GET /sync/gaps`. Referral gaps use **schema v1** (`spice_referral_compliance`): a gap
fires only when the rule-engine **recommended** one thing and the CHW **actually** did
something different.

Reference: [W12_SDK_INTEGRATION.md](./W12_SDK_INTEGRATION.md),
[TELEMETRY_CONTRACT.md](./TELEMETRY_CONTRACT.md).

## Schema v1 — referral compliance (`spice_referral_compliance`)

```json
{
  "schema_version": 1,
  "evaluator": "spice_referral_compliance",
  "when": { "op": "and", "conditions": [] },
  "metadata": {}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `schema_version` | yes | Must be `1` |
| `evaluator` | yes | Must be `spice_referral_compliance` |
| `when` | yes | Root predicate (see below) |
| `metadata` | no | Reviewer hints; ignored by evaluator |

### Compliance visit state

Build before evaluating any referral gap:

| Branch | Source (SPICE / SDK) | Keys |
|--------|----------------------|------|
| `recommended` | Rule engine at screening / assessment complete (`onScreeningCompleted`, `AssessmentEntity`) | `isReferred`, `referralStatus`, `referredReason` (list), `referralUrgency` (`URGENT` \| `NON_URGENT` \| null), `referralFacilityType`, nested `assessmentDetails.*` |
| `actual` | CHW referral submission (`onReferralSubmitted`, `ReferPatientResult`) | `didRefer`, `isUrgent`, `referralReasons` (list), `destinationTier`, `referredSiteId`, `referralFacilityType` |

Path prefix determines branch: `recommended.referredReason`, `actual.isUrgent`, etc.

**Gap fires when `when` is true** — i.e. recommendation and CHW action **differ** on the
dimension the gap describes. Matching recommendation + matching CHW choice → gap does **not**
fire.

### Logical combinators

| `op` | Fields | True when |
|------|--------|-----------|
| `and` | `conditions` | All children true |
| `or` | `conditions` | Any child true |
| `not` | `condition` | Child false |

### Recommendation precondition (rule engine side)

| `op` | Fields | True when |
|------|--------|-----------|
| `eq` | `path`, `value` | Resolved value equals `value` |
| `neq` | `path`, `value` | Resolved value differs from `value` |
| `exists` | `path` | Non-null, non-empty |
| `contains_any` | `path`, `values` | List at `path` intersects `values` |
| `contains_all` | `path`, `values` | List contains every value |
| `array_nonempty` | `path` | List length > 0 |
| `map_key_nonempty` | `path`, `key` | Map at `path` has non-empty list at `key` |
| `array_contains_substring` | `path`, `value` | Any list element contains substring |

Paths use the `recommended.` prefix (and nested `recommended.assessmentDetails.*`).

### Mismatch operators (CHW deviated from recommendation)

| `op` | Fields | True when |
|------|--------|-----------|
| `missed_referral` | — | `recommended.isReferred` is true and `actual.didRefer` is false |
| `mismatch_eq` | `recommended_path`, `actual_path` | Values differ (including one null) |
| `mismatch_contains_any` | `recommended_path`, `actual_path`, `values` | Recommended list intersects `values`, actual list does **not** |
| `mismatch_urgency` | `recommended_urgency`, `actual_path` | Rule engine urgency (`URGENT` / `NON_URGENT`) ≠ `actual.isUrgent` mapping |

`mismatch_urgency` fields:

- `recommended_urgency`: `URGENT` or `NON_URGENT` (from `recommended.referralUrgency` or derived from `highRiskPregnantWoman` / `motherRisks` keys)
- `actual_path`: typically `actual.isUrgent` (boolean)

Mapping: `URGENT` ↔ `actual.isUrgent == true`, `NON_URGENT` ↔ `actual.isUrgent == false`.

### RMNCH recommended paths

| Path | Meaning |
|------|---------|
| `recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.URGENT` | ANC emergency conditions |
| `recommended.assessmentDetails.anc.summary.highRiskPregnantWoman.NON_URGENT` | ANC non-emergency |
| `recommended.assessmentDetails.anc.summary.gapsInAnc` | ANC care gaps |
| `recommended.assessmentDetails.pncMother.motherRisks.URGENT` | PNC emergency |
| `recommended.assessmentDetails.pncMother.motherRisks.NON_URGENT` | PNC non-emergency |
| `recommended.assessmentDetails.pncMother.pncGaps` | PNC gaps |

Use exact SPICE display strings in `values` arrays.

## Typical gap pattern (reason cluster)

```json
{
  "schema_version": 1,
  "evaluator": "spice_referral_compliance",
  "when": {
    "op": "or",
    "conditions": [
      {
        "op": "and",
        "conditions": [
          {
            "op": "contains_any",
            "path": "recommended.referredReason",
            "values": ["Pneumonia", "Cough"]
          },
          {
            "op": "missed_referral"
          }
        ]
      },
      {
        "op": "mismatch_contains_any",
        "recommended_path": "recommended.referredReason",
        "actual_path": "actual.referralReasons",
        "values": ["Pneumonia", "Cough"]
      }
    ]
  }
}
```

Fires when the rule engine recommended pneumonia/cough referral but the CHW did not refer,
or referred with reasons that do not include that cluster.

## Typical gap pattern (urgency)

```json
{
  "op": "and",
  "conditions": [
    {
      "op": "map_key_nonempty",
      "path": "recommended.assessmentDetails.anc.summary.highRiskPregnantWoman",
      "key": "URGENT"
    },
    {
      "op": "mismatch_urgency",
      "recommended_urgency": "URGENT",
      "actual_path": "actual.isUrgent"
    }
  ]
}
```

## Evaluation pseudocode (compliance)

```
function evaluateGap(rule, state) -> bool:
    if rule.schema_version != 1 or rule.evaluator != "spice_referral_compliance":
        return false
    return evalPredicate(rule.when, state)

function evalMissedReferral(state) -> bool:
    return state.recommended.isReferred == true and state.actual.didRefer == false

function evalMismatchContainsAny(rec_path, act_path, values, state) -> bool:
    rec = resolve(state, rec_path)
    act = resolve(state, act_path)
    if not (is_list(rec) and any(x in values for x in rec)):
        return false
    return not (is_list(act) and any(x in values for x in act))

function evalMismatchUrgency(rec_urgency, act_path, state) -> bool:
    expected = (rec_urgency == "URGENT")
    actual = resolve(state, act_path)
    return actual is null or actual != expected
```

## Seed data

[`seed/behavioural_gaps_referral.json`](../seed/behavioural_gaps_referral.json)

Migration **0014** (`infra/alembic/versions/0014_seed_behavioural_gaps_referral.py`) upserts
these rows into `behavioural_gap` on `alembic upgrade head` (no-op if the seed file is absent).

```bash
uv run alembic -c infra/alembic.ini upgrade head
```

To refresh rows manually without re-running migrations:

```bash
uv run python bin/seed_behavioural_gaps.py --file seed/behavioural_gaps_referral.json
```
