"""Variable builders for assessment topic classification prompt."""

from __future__ import annotations

import json
from typing import Any


def build_assessment_topic_classification_variables(
    *,
    max_topics: int,
    allowed_topics: list[str],
    module_payload: dict[str, Any],
    search_metadata: dict[str, Any] | None,
) -> dict[str, str]:
    topics = allowed_topics or []
    return {
        "max_topics": str(max_topics),
        "allowed_topics_line": ", ".join(topics),
        "allowed_topics_json": json.dumps(topics, ensure_ascii=False, indent=2),
        "search_metadata_json": json.dumps(search_metadata or {}, ensure_ascii=False, indent=2),
        "module_payload_json": json.dumps(module_payload, ensure_ascii=False, indent=2),
    }
