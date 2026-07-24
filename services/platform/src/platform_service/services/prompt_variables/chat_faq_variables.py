"""Variable builders for chat FAQ prompt."""

from __future__ import annotations

import json
from typing import Any

from mc_foundation.locale import locale_display_name

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import render_locale_map_field_schema


def build_chat_faq_variables(
    *,
    target_count: int,
    clusters_payload: list[dict[str, Any]],
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> dict[str, str]:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context
    primary_label = locale_display_name(primary_locale)
    return {
        "target_count": str(target_count),
        "deployment_region_context": region_context,
        "primary_locale_label": primary_label,
        "question_field_schema": render_locale_map_field_schema(
            "question",
            primary_locale=primary_locale,
            primary_required=True,
            description="FAQ chip text",
        ),
        "clusters_json": json.dumps(clusters_payload, ensure_ascii=False, indent=2),
    }
