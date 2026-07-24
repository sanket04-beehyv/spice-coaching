"""Variable builders for module identifier prompt."""

from __future__ import annotations

import json
from typing import Any

from mc_foundation.locale import get_locale_metadata, locale_display_name

from platform_service.config import get_settings
from platform_service.module_domains import module_domain_catalog_for_prompt
from platform_service.services.prompt_id_codec import PromptIdCodec
from platform_service.services.prompts.module_identifier_prompt import (
    _BRANCH_CLINICAL,
    _BRANCH_CLINICAL_WITH_APP_WORKFLOWS,
    _BRANCH_DIGITAL,
    _CARDINALITY_CARDS_ONLY_SECTION,
    _CARDINALITY_GUIDANCE_SYSTEM_SECTION,
    _CARDINALITY_QUIZZES_ONLY_SECTION,
    _CONTENT_UPDATE_FIELDS,
    _INGESTION_GUIDANCE_SYSTEM_SECTION,
    _INGESTION_RATIONALE_JSON_FIELD,
)
from platform_service.services.prompts.symbol_verbalization import render_locale_map_field_schema


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
    if not content_domains:
        return _BRANCH_CLINICAL
    if content_domains == {"digital"}:
        return _BRANCH_DIGITAL
    if content_domains == {"clinical_with_app_workflows"}:
        return _BRANCH_CLINICAL_WITH_APP_WORKFLOWS
    return _BRANCH_CLINICAL


def _cardinality_guidance_section(
    *,
    target_cards: int | None,
    target_quizzes: int | None,
) -> str:
    if target_cards is not None and target_quizzes is not None:
        return _CARDINALITY_GUIDANCE_SYSTEM_SECTION.format(
            target_cards=target_cards,
            target_quizzes=target_quizzes,
        )
    if target_cards is not None:
        return _CARDINALITY_CARDS_ONLY_SECTION.format(target_cards=target_cards)
    if target_quizzes is not None:
        return _CARDINALITY_QUIZZES_ONLY_SECTION.format(target_quizzes=target_quizzes)
    return ""


def _count_schema_text(lo: int, hi: int) -> str:
    if lo == hi:
        return str(lo)
    return f"{lo}-{hi}"


def build_module_identifier_system_variables(
    *,
    content_domains: set[str],
    card_min_count: int | None = None,
    card_max_count: int | None = None,
    quiz_min_count: int | None = None,
    quiz_max_count: int | None = None,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
    ingestion_instructions: str | None = None,
    target_cards: int | None = None,
    target_quizzes: int | None = None,
) -> dict[str, str]:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context
    card_lo = card_min_count if card_min_count is not None else settings.card_min_count
    card_hi = card_max_count if card_max_count is not None else settings.card_max_count
    quiz_lo = quiz_min_count if quiz_min_count is not None else settings.quiz_min_questions
    quiz_hi = quiz_max_count if quiz_max_count is not None else settings.quiz_max_questions

    if ingestion_instructions:
        ingestion_guidance_section = _INGESTION_GUIDANCE_SYSTEM_SECTION
        ingestion_rationale_field = _INGESTION_RATIONALE_JSON_FIELD
    else:
        ingestion_guidance_section = ""
        ingestion_rationale_field = ""

    primary_label = locale_display_name(primary_locale)
    description_desc = f"one paragraph, ~2-4 sentences ({primary_label})"
    return {
        "deployment_region_context": region_context,
        "annexure_terms": _annexure_terms_phrase(primary_locale),
        "module_domain_catalog": module_domain_catalog_for_prompt(),
        "ingestion_guidance_section": ingestion_guidance_section,
        "cardinality_guidance_section": _cardinality_guidance_section(
            target_cards=target_cards,
            target_quizzes=target_quizzes,
        ),
        "content_domain_branch_instructions": _branch_instructions_for(content_domains),
        "ingestion_rationale_field": ingestion_rationale_field,
        "content_update_fields": _CONTENT_UPDATE_FIELDS,
        "description_field_schema": render_locale_map_field_schema(
            "description",
            primary_locale=primary_locale,
            description=description_desc,
            primary_required=True,
        ),
        "card_count_schema": _count_schema_text(card_lo, card_hi),
        "quiz_count_schema": _count_schema_text(quiz_lo, quiz_hi),
    }


def build_module_identifier_human_variables(
    *,
    already_published_modules: list[dict[str, Any]],
    document_outlines: list[dict[str, Any]],
    page_corpus: list[dict[str, Any]],
    codec: PromptIdCodec,
    ingestion_instructions: str | None = None,
) -> dict[str, str]:
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

    tail = {"document_outlines_repeat": document_outlines}
    ingestion_suffix = ""
    if ingestion_instructions:
        ingestion_suffix = (
            "\n\nUSER_INGESTION_GUIDANCE (hard scope filter — only emit candidates "
            "grounded in this guidance AND cited corpus blocks):\n"
            f"{ingestion_instructions.strip()}"
        )

    return {
        "head_json": json.dumps(head, ensure_ascii=False, indent=2),
        "corpus_body": "\n".join(body_lines),
        "tail_json": json.dumps(tail, ensure_ascii=False, indent=2),
        "ingestion_suffix": ingestion_suffix,
    }


def build_module_identifier_variables(
    *,
    already_published_modules: list[dict[str, Any]],
    document_outlines: list[dict[str, Any]],
    page_corpus: list[dict[str, Any]],
    codec: PromptIdCodec,
    content_domains: set[str],
    card_min_count: int | None = None,
    card_max_count: int | None = None,
    quiz_min_count: int | None = None,
    quiz_max_count: int | None = None,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
    ingestion_instructions: str | None = None,
    target_cards: int | None = None,
    target_quizzes: int | None = None,
) -> dict[str, str]:
    system_vars = build_module_identifier_system_variables(
        content_domains=content_domains,
        card_min_count=card_min_count,
        card_max_count=card_max_count,
        quiz_min_count=quiz_min_count,
        quiz_max_count=quiz_max_count,
        deployment_primary_locale=deployment_primary_locale,
        deployment_region_context=deployment_region_context,
        ingestion_instructions=ingestion_instructions,
        target_cards=target_cards,
        target_quizzes=target_quizzes,
    )
    human_vars = build_module_identifier_human_variables(
        already_published_modules=already_published_modules,
        document_outlines=document_outlines,
        page_corpus=page_corpus,
        codec=codec,
        ingestion_instructions=ingestion_instructions,
    )
    return {**system_vars, **human_vars}
