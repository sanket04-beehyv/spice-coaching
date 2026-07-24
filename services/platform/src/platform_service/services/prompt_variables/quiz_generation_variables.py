"""Variable builders for quiz generation prompt."""

from __future__ import annotations

from platform_service.config import Settings, get_settings
from platform_service.services.prompts.symbol_verbalization import (
    render_locale_list_map_field_schema,
    render_locale_map_field_schema,
)


def build_quiz_generation_variables(
    *,
    module_title: str,
    domain: str,
    quiz_size: int,
    cards_block: str,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
    settings: Settings | None = None,
) -> dict[str, str]:
    s = settings or get_settings()
    primary_locale = deployment_primary_locale or s.deployment_primary_locale
    region_context = deployment_region_context or s.deployment_region_context
    return {
        "deployment_region_context": region_context,
        "primary_locale": primary_locale,
        "case_setup_field_schema": render_locale_map_field_schema(
            "case_setup",
            primary_locale=primary_locale,
            description="patient case, ~2 sentences",
        ),
        "question_field_schema": render_locale_map_field_schema(
            "question",
            primary_locale=primary_locale,
            primary_required=True,
        ),
        "options_field_schema": render_locale_list_map_field_schema(
            "options",
            primary_locale=primary_locale,
            max_items=4,
            description="exactly 4 options",
        ),
        "explanation_field_schema": render_locale_map_field_schema(
            "explanation",
            primary_locale=primary_locale,
            description="why the correct answer is correct; no card references",
        ),
        "module_title": module_title,
        "domain": domain,
        "quiz_size": str(quiz_size),
        "cards_block": cards_block,
    }
