"""Text similarity helpers for module title and card fingerprint matching."""

from __future__ import annotations


def normalise_title(title: str) -> str:
    """Lowercase + collapse whitespace for title-equality grouping."""
    return " ".join((title or "").strip().lower().split())


def _trigrams(s: str) -> set[str]:
    s = normalise_title(s)
    if len(s) < 3:
        return {s} if s else set()
    return {s[i : i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity over character trigrams. Returns 0.0..1.0."""
    ta = _trigrams(a)
    tb = _trigrams(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0
