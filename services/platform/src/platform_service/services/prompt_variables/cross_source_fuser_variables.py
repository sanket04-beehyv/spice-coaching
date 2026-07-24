"""Variable builders for cross-source fuser prompt."""

from __future__ import annotations

import json
from typing import Any


def build_cross_source_fuser_variables(*, candidates: list[dict[str, Any]]) -> dict[str, str]:
    payload = {
        "candidates": [
            {
                "id": str(c.get("id")),
                "source_document_id": str(c.get("source_document_id")),
                "title": c.get("proposed_title", ""),
                "scope_summary": c.get("scope_summary", ""),
            }
            for c in candidates
        ]
    }
    return {
        "candidates_json": json.dumps(payload, ensure_ascii=False, indent=2),
    }
