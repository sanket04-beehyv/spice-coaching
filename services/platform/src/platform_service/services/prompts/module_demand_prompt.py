"""Admin module demand LLM fallback helpers."""

from __future__ import annotations


def fallback_summary(*, available_count: int, unavailable_count: int, top_k: int) -> str:
    if available_count == 0 and unavailable_count == 0:
        return "No module training requests have been submitted yet."
    return (
        f"Top {top_k} module demand: {available_count} available "
        f"(ready to assign or in draft) and {unavailable_count} not yet in the catalog."
    )
