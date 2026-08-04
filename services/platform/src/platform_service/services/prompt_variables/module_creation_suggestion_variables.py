"""Variable builders for module creation suggestion prompt."""

from __future__ import annotations

import json
from typing import Any


def build_module_creation_suggestion_variables(
    *,
    max_suggestions: int,
    suggestion_date: str,
    draft_catalog: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    requests: list[dict[str, Any]],
) -> dict[str, str]:
    return {
        "max_suggestions": str(max_suggestions),
        "suggestion_date": suggestion_date,
        "draft_catalog_json": json.dumps(draft_catalog, ensure_ascii=False),
        "questions_json": json.dumps(questions, ensure_ascii=False),
        "requests_json": json.dumps(requests, ensure_ascii=False),
    }
