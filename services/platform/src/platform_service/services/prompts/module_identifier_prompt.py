"""Stage 2 — module-identification prompt (per-content-domain variants).

Per `docs/ARCHITECTURE_RESET.md`. The prompt instructs the LLM to identify
behavioural-TOPIC modules from a corpus, with branch logic per
source_document.content_domain:

- clinical → emit initial_training candidates for comprehensive training manuals
- supervisor_update → emit content_update candidates with previous/current/
  rationale fields populated
- digital → emit digital_proficiency candidates
- clinical_with_app_action → clinical content tied to in-app workflows

The architecture-reset removed the gap-driven prompting from this stage:
gaps are runtime telemetry, not source-document structure. The pipeline
proposes topic-modules; the reviewer/admin manually maps modules to gaps
via the dashboard (`module_trigger_binding`).

The prompt is versioned via the (template_id, template_version) pair
recorded on the ai-runtime InferenceRequest. Bumping the version invalidates
llm_call_cache entries from the prior version on re-run.
"""

from __future__ import annotations

import json
from typing import Any

from mc_foundation.locale import get_locale_metadata, locale_display_name

from platform_service.config import get_settings
from platform_service.services.prompt_id_codec import PromptIdCodec
from platform_service.services.prompts.symbol_verbalization import render_locale_map_field_schema

MODULE_IDENTIFIER_TEMPLATE_ID = "v33-stage-c-module-identifier"
# v6: content_domain replaces authority_kind; added clinical_with_app_action branch.
# v7: add bilingual module descriptions (locale-keyed description map).
# v8: optional admin ingestion steering guidance (USER_INGESTION_GUIDANCE).
# v9: ingestion guidance is a hard scope filter; per-candidate
#     ingestion_instruction_rationale when guidance is present.
# v10: ingestion guidance + rationale field only when USER_INGESTION_GUIDANCE
#      is present — restores dev/v7-equivalent prompts for normal ingests.
# v12: monolingual deployment — primary locale only.
MODULE_IDENTIFIER_TEMPLATE_VERSION = 12

_INGESTION_GUIDANCE_SYSTEM_SECTION = """\
INGESTION GUIDANCE MODE (USER_INGESTION_GUIDANCE is present in the human message):
- Treat it as a HARD scope filter for module selection — NOT as a title tweak
  or soft preference. Only emit candidates whose topic is directly requested or
  clearly implied by the guidance AND grounded in cited corpus blocks.
- If no corpus-grounded topics satisfy the guidance, return
  {{"candidates": []}}. Do NOT fall back to general corpus modules.
- Guidance MUST NOT override: DO NOT invent topics, annexure exclusion, grouping
  rules, citation token format, or content-domain branching.
- For EACH emitted candidate, populate ingestion_instruction_rationale with
  1-3 sentences explaining which part of the guidance the candidate satisfies
  and how the cited corpus supports it.
"""

_INGESTION_RATIONALE_JSON_FIELD = (
    '  "ingestion_instruction_rationale": "string — 1-3 sentences explaining '
    "which part of the guidance this candidate satisfies and how the cited "
    'corpus supports it",\n'
)

_SYSTEM_PROMPT_BASE = """\
You are drafting BEHAVIOURAL TOPIC modules for community health workers (CHWs)
in {deployment_region_context}.

A module covers ONE actionable behavioural topic the CHW must internalise correctly. Examples:
- "Correct ANC referral by risk category"
- "Recognising postpartum danger signs"
- "Hypertension identification and management" (when the source has a dedicated HTN chapter)
- "Dengue fever recognition, prevention, and referral"
- "Effective communication and counselling skills" (when the source has a dedicated chapter)
- "BRAC field activities and follow-up workflow" (when the source has an operational chapter)
- "SPICE form submission failure recovery"

GROUPING RULES — do NOT over-fragment, do NOT under-emit:

1. Do NOT create modules per individual test, vital sign, lab value, or
   measurement threshold (e.g. don't make separate modules for "BP measurement",
   "Hb measurement", "blood-glucose threshold"). Group related measurements
   into a parent procedural unit (e.g. "Performing antenatal physical and
   pathological examinations").

2. DO create a standalone module per NAMED DISEASE or DEDICATED CHAPTER
   the source treats as its own learning unit. If the source corpus has a
   chapter on Hypertension, Diabetes, Tuberculosis, Malaria, Cancer, Dengue,
   Diarrhoea, ARI/Pneumonia, etc., emit a dedicated module for it — even
   when the chapter overlaps with an adjacent screening or measurement
   chapter. The CHW's ongoing-management knowledge for the disease is
   distinct from the one-shot screening procedure.

3. DO create a module for non-clinical CHW skill chapters: communication,
   counselling skills, field activities, reporting workflow, safeguarding.
   These are CHW practice topics even though they are not disease-management.
   Don't deprioritise them just because they aren't clinical.

4. DO NOT propose modules from annexures, appendices, or reference
   sections. Forms, checklists, reporting templates, consent forms, and
   reference tables (e.g. "Healthcare Services by Facility Level") are
   JOB AIDS — the CHW fills them out or looks at them on the job, not
   topics they internalise through training. Detection cues:
   - Page or section heading begins with {annexure_terms}
     or similar.
   - Content is dominated by blank fields, tick-box rows, signature
     lines, or columnar reference data the user fills in or looks up.
   The training-content equivalent (e.g. "How to fill the NCD reporting
   form" as a procedural lesson) IS a valid module — the line is between
   the form itself (job aid) and the procedure of using it (trainable).

DO NOT invent topics. Only group and label content present in the source corpus.

{ingestion_guidance_section}
You are receiving:
1. Document outline — section structure with page ranges
2. Already-published modules — DO NOT duplicate
3. Per-page corpus content — markdown text + content_block_ids

Content-domain branching:
{content_domain_branch_instructions}

CITATION FORMAT — READ CAREFULLY:

The CORPUS section below uses SHORT TOKENS to identify documents, pages,
and content blocks instead of full UUIDs. You will see headers like:

    ### source_document_id=d1 content_domain=...
    #### source_page_id=p47 page_number=12
    [content_block_id=b231 block_type=paragraph]

In your `source_provenance` output, use the SAME short tokens — `d1`,
`p47`, `b231` — exactly as they appear. DO NOT invent UUIDs. DO NOT
expand the tokens. Copy them character-for-character.

For EACH candidate module, return a JSON object with these fields:
{{
  "proposed_title": "string — short topic title in the deployment primary locale",
  "scope_summary": "string — one paragraph, ~3-5 sentences",
{description_field_schema}
  "source_provenance": [
    {{
      "source_document_id": "short token like 'd1' (NOT a UUID)",
      "source_page_id": "short token like 'p47' (NOT a UUID)",
      "content_block_ids": ["short token like 'b231'", "..."]
    }},
    ...
  ],
  "estimated_card_count": integer (3-7),
  "estimated_quiz_count": integer (3-10),
  "proposed_module_type": "refresher" | "content_update" | "digital_proficiency" | "initial_training",
  "clinical_review_notes": "string — what the reviewer should validate",
{ingestion_rationale_field}  {content_update_fields}
}}

Return STRICT JSON. The output must be a single JSON object with this top-level shape:
{{
  "candidates": [
    {{ ... candidate object ... }},
    ...
  ]
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


_BRANCH_CLINICAL = """\
This corpus contains CLINICAL training / guideline content (current correct
protocol). Emit modules with proposed_module_type = "initial_training".
These are comprehensive training materials that teach foundational clinical
knowledge and procedures. The previous_practice_summary, current_practice_summary,
rationale_summary fields should be empty/null for these candidates."""

_BRANCH_SUPERVISOR_UPDATE = """\
This corpus contains a SUPERVISOR UPDATE (a recent change in protocol or guidance).
Emit modules with proposed_module_type = "content_update". Each candidate MUST extract:
- previous_practice_summary: what was done before the change
- current_practice_summary: what is done now
- rationale_summary: why the change happened

The candidate's scope_summary captures the overall topic; the three summaries
above carry the delta-specific framing."""

_BRANCH_DIGITAL = """\
This corpus contains DIGITAL workflow content. Emit modules with
proposed_module_type = "digital_proficiency". Card body will be procedural
(workflow steps), not clinical. Cite content_blocks of type 'figure' for
screenshots or workflow diagrams when present."""

_BRANCH_CLINICAL_WITH_APP_ACTION = """\
This corpus contains CLINICAL content tied to in-app actions (e.g. UHIS/SPICE
workflows embedded in clinical training). Emit modules with
proposed_module_type = "initial_training". Emphasise the clinical decision or
procedure AND the corresponding app steps the CHW performs. Card body should
pair clinical rationale with actionable workflow steps. Cite content_blocks of
type 'figure' for app screenshots or workflow diagrams when present."""

_BRANCH_MIXED = """\
This corpus contains MIXED content domains. For each candidate, choose
proposed_module_type based on which source documents its provenance comes from:
- if ANY cited block is from a supervisor_update document → content_update
  (and you MUST extract previous/current/rationale summaries)
- if all cited blocks are from digital-domain documents → digital_proficiency
- if all cited blocks are from clinical_with_app_action documents → initial_training
  (clinical + app workflow framing per the clinical_with_app_action branch)
- otherwise → initial_training
"""


_CONTENT_UPDATE_FIELDS = """\
"previous_practice_summary": "string or null — what was done before (content_update only)",
  "current_practice_summary": "string or null — what is done now (content_update only)",
  "rationale_summary": "string or null — why it changed (content_update only)\""""


def _annexure_terms_phrase(primary_locale: str) -> str:
    terms: list[str] = []
    for term in get_locale_metadata(primary_locale).annexure_terms:
        if term not in terms:
            terms.append(term)
    if not terms:
        return '"Annexure", "Appendix", or similar'
    quoted = ", ".join(f'"{term}"' for term in terms)
    return f"{quoted}, or similar"


def _branch_instructions_for(content_domains: set[str]) -> str:
    """Pick the correct branch text for the workspace's content-domain mix."""
    if not content_domains:
        return _BRANCH_CLINICAL
    if "supervisor_update" in content_domains and len(content_domains) > 1:
        return _BRANCH_MIXED
    if content_domains == {"supervisor_update"}:
        return _BRANCH_SUPERVISOR_UPDATE
    if content_domains == {"digital"}:
        return _BRANCH_DIGITAL
    if content_domains == {"clinical_with_app_action"}:
        return _BRANCH_CLINICAL_WITH_APP_ACTION
    return _BRANCH_CLINICAL


def render_system_prompt(
    content_domains: set[str],
    *,
    ingestion_instructions: str | None = None,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> str:
    """Render the system prompt with the content-domain-specific branch."""
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context

    if ingestion_instructions:
        ingestion_guidance_section = _INGESTION_GUIDANCE_SYSTEM_SECTION
        ingestion_rationale_field = _INGESTION_RATIONALE_JSON_FIELD
    else:
        ingestion_guidance_section = ""
        ingestion_rationale_field = ""

    primary_label = locale_display_name(primary_locale)
    description_desc = f"one paragraph, ~2-4 sentences ({primary_label})"
    description_field_schema = render_locale_map_field_schema(
        "description",
        primary_locale=primary_locale,
        description=description_desc,
        primary_required=True,
    )

    return _SYSTEM_PROMPT_BASE.format(
        deployment_region_context=region_context,
        annexure_terms=_annexure_terms_phrase(primary_locale),
        ingestion_guidance_section=ingestion_guidance_section,
        content_domain_branch_instructions=_branch_instructions_for(content_domains),
        ingestion_rationale_field=ingestion_rationale_field,
        content_update_fields=_CONTENT_UPDATE_FIELDS,
        description_field_schema=description_field_schema,
    )


def render_human_message(
    *,
    already_published_modules: list[dict[str, Any]],
    document_outlines: list[dict[str, Any]],
    page_corpus: list[dict[str, Any]],
    codec: PromptIdCodec,
    ingestion_instructions: str | None = None,
) -> str:
    """Render the human-message payload.

    Layout (mitigating "lost in the middle"):
    - Outline + already-published modules at the START (anchor)
    - Corpus markdown in the MIDDLE (largest section)
    - Outline repeated at the END (re-anchor)

    The corpus section uses short ordinal tokens (`d1`, `p47`, `b231`)
    instead of full UUIDs — Gemini cannot reliably copy 36-character
    hex strings, but copies short tokens character-perfect. The codec
    bidirectional map is used in the identifier service to translate
    the LLM's response back to real UUIDs before validation.
    """
    head = {
        "already_published_modules": already_published_modules,
        "document_outlines": document_outlines,
    }
    body_lines = ["## CORPUS ##"]
    if codec.page_count == 1:
        body_lines.append(
            "\nNOTE: This corpus has exactly ONE page. The only valid source_page_id "
            "token is p1 — do NOT use page_number values or block indices as page "
            "tokens. Cite content blocks with b{n} tokens from the corpus headers."
        )
    for doc in page_corpus:
        body_lines.append(
            f"\n### source_document_id={codec.doc_token(doc['source_document_id'])} "
            f"content_domain={doc.get('content_domain', 'unknown')} "
            f"primary_language={doc.get('primary_language', 'unknown')}"
        )
        for page in doc.get("pages", []):
            body_lines.append(
                f"\n#### source_page_id={codec.page_token(page['source_page_id'])} "
                f"page_number={page['page_number']}"
            )
            for block in page.get("blocks", []):
                body_lines.append(
                    f"\n[content_block_id={codec.block_token(block['content_block_id'])} "
                    f"block_type={block['block_type']}]\n{block['content_text']}"
                )

    tail = {
        "document_outlines_repeat": document_outlines,
    }

    message = (
        json.dumps(head, ensure_ascii=False, indent=2)
        + "\n\n"
        + "\n".join(body_lines)
        + "\n\n"
        + json.dumps(tail, ensure_ascii=False, indent=2)
    )
    if ingestion_instructions:
        message += (
            "\n\n## USER_INGESTION_GUIDANCE ##\n"
            "<<<BEGIN_ADMIN_STEERING>>>\n"
            f"{ingestion_instructions}\n"
            "<<<END_ADMIN_STEERING>>>"
        )
    return message
