# Golden dataset for RAG retrieval evaluation

`rag-eval` is a uv workspace member (`eval/`). Batch mode (`uv run python -m eval.rag` or `uv run rag-eval`) evaluates retrieval against a golden JSON **array** of records. The default dataset is `golden_dataset.json`. Use `--method bm25` (default), `--method embedding`, or `--method rag`.

## Schema (`golden_dataset.json`)

```json
[
  {
    "id": "Q001",
    "question_bn": "ব্র্যাক স্বাস্থ্য কর্মসূচি - রুরাল এর মূল ভিত্তি কী?",
    "expected_answer_bn": "ব্র্যাক স্বাস্থ্য কর্মসূচি - রুরাল বিশ্ব স্বাস্থ্য সংস্থার প্রাথমিক স্বাস্থ্য পরিচর্যা তত্ত্বের আলোকে সাজানো হয়েছে।",
    "source_card_id": ["d492130f-b4b3-4374-93d7-28dd7b4fa519"],
    "query_type": "Factual",
    "linguistic_variation": "Standard Written Bengali",
    "chw_pattern": "None",
    "answerable": "yes",
    "confidence": "high",
    "module_id": ["6d22a857-9d62-451e-8a6e-d820239b0d2e"]
  }
]
```

| Field | Required | Notes |
|-------|----------|-------|
| `question_bn` | yes | Bengali question (one eval row per record) |
| `expected_answer_bn` | yes | Gold answer for RAG scoring |
| `module_id` | yes | Published module UUID(s); `[]` for out-of-scope |
| `source_card_id` | no | `module_card.id` UUID(s) within the expected module(s); `[]` for Negative |
| `query_type` | no | Report category: Factual, Situational, Procedural, Negative, Ambiguous |
| `answerable` | no (default `yes`) | `yes`, `no`, or `partial` |
| `id` | no | Stable identifier (default: `q_001`, `q_002`, …) |
| `linguistic_variation`, `chw_pattern`, `confidence` | no | Metadata (included in per-record artifacts; not aggregated yet) |

**250 Bengali records** — no en/bn row doubling at load time.

### `answerable` semantics

| Value | Meaning |
|-------|---------|
| `yes` | In-scope; evaluated for retrieval and RAG answer quality |
| `no` | Out-of-scope (Negative); model should refuse; skipped in BM25/embedding batch |
| `partial` | In-scope but ambiguous; model should clarify missing context |

Out-of-scope when `answerable` is `no` or `module_id` is empty.

### BM25 pipeline evaluation (module + card)

For batch runs with `--method bm25`:

1. **Module retrieval** — global BM25 over published modules; metrics compare top-k module IDs to `module_id`.
2. **Card retrieval (pipeline)** — BM25 over cards in the **top-1 retrieved module** only; metrics compare top-k card IDs to `source_card_id` (any listed card counts as a hit).

Card pipeline runs only when exactly one `module_id` and at least one `source_card_id` are present.

## Legacy schemas

Older formats remain supported for unit-test fixtures.

**Bilingual v2** (`expected_module_id` + `question.en`/`question.bn` or `question_en`/`question_bn`):

Each entry expands into two eval rows (`_en` and `_bn`).

**Title-based module labels**:

```json
{
  "question": "A mother reports her child has had a cough for 35 days…",
  "expected_module": "Management of Acute Respiratory Infection (ARI)"
}
```

**Explicit module UUIDs**:

```json
{
  "id": "q_001",
  "category": "factual_simple",
  "question": "…",
  "relevant_module_ids": ["35f2887d-7824-4b92-845a-a4ca299879e3"],
  "is_answerable": true
}
```

**Legacy RAG format** (`query`, `language`, `expected_answer`, `expected_module_id`):

Used by unit tests; not the on-disk canonical dataset.

## Usage

### BM25 (in-memory lexical search)

```bash
# Single query against live DB corpus
uv run python -m eval.rag "ANC referral criteria" --k 5

# Batch BM25 module + card pipeline metrics (default dataset)
uv run python -m eval.rag --k 5 --output eval/rag/reports/golden-bm25-run.json
```

Requires `DATABASE_URL` (see repo `.env.example`) and published modules in Postgres.

### Embedding (ai-runtime + pgvector cosine distance)

```bash
uv run python -m eval.rag --method embedding --k 5 \
  --output eval/rag/reports/embedding-run.json
```

Module-level metrics only (no card pipeline).

### RAG chatbot E2E

Evaluates the full `/coaching/rag-query` pipeline in-process via `CoachingRagService`. Default dataset is `golden_dataset.json` (250 records).

#### Metrics by pipeline stage

| Stage | Metrics |
|-------|---------|
| **Retrieval** | `hit_at_k`, `mrr`, `precision_at_k`, `recall_at_k`, `ndcg_at_k`, `gold_rank`, `gold_cosine_distance`, `retrieval_miss` |
| **Context (proxy)** | `gold_card_hit`, `card_recall_at_k`, `card_mrr` — gold `source_card_id` values vs cards in retrieved modules |
| **Citation** | `strict_citation_accuracy`, `citation_or_retrieval_accuracy`, `citation_precision`, `citation_recall`, `spurious_citation`, `uncited_but_answered` |
| **Answer** | `token_f1`, `token_recall`, `exact_match`, `answer_grounding_overlap`, `partial_answer_correct`, `abstention_correct`, `json_parse_success`, `empty_answer` |
| **LLM judge** | `faithfulness`, `answer_relevance`, `groundedness` (optional extra generate call per record) |
| **Performance** | E2E / generate / embed latency percentiles, token usage |

```bash
# Full batch (250 RAG + 250 judge calls when --llm-judge is on — slow)
uv run python -m eval.rag --method rag --k 5 \
  --output eval/rag/reports/golden-rag-run.json

# Smoke test without judge cost
uv run python -m eval.rag --method rag --limit 5 --no-llm-judge

# Single record with judge
uv run python -m eval.rag --method rag --record-id Q001 --llm-judge

# Override judge model
uv run python -m eval.rag --method rag --record-id Q001 --judge-model gemini-2.5-flash
```

`--llm-judge` is on by default for `--method rag`. Use `--no-llm-judge` for faster smoke runs.

Reports: JSON plus Markdown scorecard under `eval/rag/reports/` with per-stage sections (Retrieval, Context, Citation, Answer, LLM Judge, Performance, Errors, Per-category, By answerable).
