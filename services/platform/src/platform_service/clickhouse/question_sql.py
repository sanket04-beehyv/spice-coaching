"""ClickHouse SQL fragments for chatbot question extract/normalize.

Mirrors ``normalize_question`` in ``platform_service.services.question_text``
for GROUP BY / dedupe over ``coaching_events.payload_json``.
"""

from __future__ import annotations

QUESTION_EXTRACT_SQL = """
coalesce(
  nullIf(JSONExtractString(payload_json, 'question'), ''),
  nullIf(JSONExtractString(payload_json, 'query'), '')
)
""".strip()

QUESTION_NORMALIZE_KEY_SQL = f"""
lowerUTF8(replaceRegexpAll(trimBoth({QUESTION_EXTRACT_SQL}), '\\\\s+', ' '))
""".strip()
