"""Variable builders for coaching RAG prompt."""

from __future__ import annotations

from mc_foundation.locale import locale_display_name

from platform_service.config import Settings


def build_coaching_rag_variables(
    *,
    question: str,
    context: str,
    lang: str,
    settings: Settings,
) -> dict[str, str]:
    lang_label = locale_display_name(lang)
    return {
        "lang_label": lang_label,
        "lang": lang,
        "question": question,
        "context": context,
    }
