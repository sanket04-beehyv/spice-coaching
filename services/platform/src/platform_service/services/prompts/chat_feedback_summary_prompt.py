"""Weekly chat feedback summary fallback helpers."""

from __future__ import annotations

from typing import Any


def _event_snippet(event: dict[str, Any]) -> str | None:
    feedback = str(event.get("feedback") or "").strip()
    if feedback:
        return feedback[:200]
    question = str(event.get("question") or "").strip()
    if question:
        return question[:200]
    return None


def fallback_summary(
    *,
    event_counts: dict[str, int],
    positive_online_events: list[dict[str, Any]],
    positive_offline_events: list[dict[str, Any]],
    negative_online_events: list[dict[str, Any]],
    negative_offline_events: list[dict[str, Any]],
) -> dict[str, Any]:
    positive = event_counts.get("positive", 0)
    positive_online = event_counts.get("positive_online", 0)
    positive_offline = event_counts.get("positive_offline", 0)
    negative_online = event_counts.get("negative_online", 0)
    negative_offline = event_counts.get("negative_offline", 0)
    total = event_counts.get("total", 0)

    if total == 0:
        summary = "No new chat feedback events in this period."
    else:
        summary = (
            f"Received {total} new chat feedback events: {positive} positive "
            f"({positive_online} online, {positive_offline} offline), "
            f"{negative_online} negative online, and {negative_offline} negative offline."
        )

    def _snippets(events: list[dict[str, Any]], limit: int = 3) -> list[str]:
        snippets: list[str] = []
        for event in events:
            snippet = _event_snippet(event)
            if snippet is None:
                continue
            snippets.append(snippet)
            if len(snippets) >= limit:
                break
        return snippets

    positive_online_themes: list[str] = []
    if positive_online > 0:
        positive_online_themes.append(
            f"{positive_online} CHW(s) marked online chat responses as helpful this period."
        )
        for snippet in _snippets(positive_online_events):
            positive_online_themes.append(f"Positive online note: {snippet}")

    positive_offline_themes: list[str] = []
    if positive_offline > 0:
        positive_offline_themes.append(
            f"{positive_offline} CHW(s) marked offline chat responses as helpful this period."
        )
        for snippet in _snippets(positive_offline_events):
            positive_offline_themes.append(f"Positive offline note: {snippet}")

    negative_online_recommendations: list[str] = []
    if negative_online > 0:
        negative_online_recommendations.append(
            f"Review {negative_online} negative online RAG feedback event(s) for retrieval or answer quality issues."
        )
        negative_online_recommendations.extend(
            f"Investigate CHW comment: {snippet}" for snippet in _snippets(negative_online_events)
        )

    negative_offline_recommendations: list[str] = []
    if negative_offline > 0:
        negative_offline_recommendations.append(
            f"Review {negative_offline} negative offline feedback event(s) for on-device content gaps."
        )
        negative_offline_recommendations.extend(
            f"Investigate CHW comment: {snippet}" for snippet in _snippets(negative_offline_events)
        )

    return {
        "llm_summary": summary,
        "positive_online_themes": positive_online_themes,
        "positive_offline_themes": positive_offline_themes,
        "negative_online_recommendations": negative_online_recommendations,
        "negative_offline_recommendations": negative_offline_recommendations,
    }
