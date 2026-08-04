"""Variable builders for gap classification prompt."""

from __future__ import annotations

import json
from typing import Any


def build_gap_classification_variables(
    *,
    max_associations: int,
    module_payload: dict[str, Any],
    registry_gaps: list[dict[str, str]],
) -> dict[str, str]:
    return {
        "max_associations": str(max_associations),
        "registry_gaps_json": json.dumps(registry_gaps, ensure_ascii=False, indent=2),
        "module_payload_json": json.dumps(module_payload, ensure_ascii=False, indent=2),
    }
