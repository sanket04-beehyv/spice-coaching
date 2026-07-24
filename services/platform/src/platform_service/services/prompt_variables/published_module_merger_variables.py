"""Variable builders for published module merger prompt."""

from __future__ import annotations

import json
from typing import Any

from mc_foundation.locale import locale_display_name

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import render_locale_map_field_schema


def build_published_module_merger_variables(
    *,
    candidate: dict[str, Any],
    new_cards: list[dict[str, Any]],
    existing_modules: list[dict[str, Any]],
    card_min_count: int,
    card_max_count: int,
    deployment_primary_locale: str | None = None,
) -> dict[str, str]:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    primary_label = locale_display_name(primary_locale)
    candidate_payload = {
        "proposed_title": candidate.get("proposed_title"),
        "scope_summary": candidate.get("scope_summary"),
        "proposed_module_type": candidate.get("proposed_module_type"),
        "previous_practice_summary": candidate.get("previous_practice_summary"),
        "current_practice_summary": candidate.get("current_practice_summary"),
        "rationale_summary": candidate.get("rationale_summary"),
    }
    return {
        "card_min_count": str(card_min_count),
        "card_max_count": str(card_max_count),
        "primary_locale_label": primary_label,
        "title_field_schema": render_locale_map_field_schema(
            "title",
            primary_locale=primary_locale,
            primary_required=True,
        ),
        "body_field_schema": render_locale_map_field_schema(
            "body",
            primary_locale=primary_locale,
            description="refresher / initial_training / digital_proficiency",
        ),
        "next_action_field_schema": render_locale_map_field_schema(
            "next_action",
            primary_locale=primary_locale,
        ),
        "previous_practice_field_schema": render_locale_map_field_schema(
            "previous_practice",
            primary_locale=primary_locale,
            description="content_update only",
        ),
        "current_practice_field_schema": render_locale_map_field_schema(
            "current_practice",
            primary_locale=primary_locale,
            description="content_update only",
        ),
        "rationale_field_schema": render_locale_map_field_schema(
            "rationale_for_change",
            primary_locale=primary_locale,
            description="content_update only",
        ),
        "candidate_json": json.dumps(candidate_payload, ensure_ascii=False, indent=2),
        "new_cards_json": json.dumps(new_cards, ensure_ascii=False, indent=2),
        "existing_modules_json": json.dumps(existing_modules, ensure_ascii=False, indent=2),
    }
