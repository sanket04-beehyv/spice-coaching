"""Variable builders for chat feedback summary prompt."""

from __future__ import annotations

import json
from typing import Any


def build_chat_feedback_summary_variables(*, payload: dict[str, Any]) -> dict[str, str]:
    return {
        "payload_json": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    }
