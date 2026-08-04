"""Module.domain normalization and prompt hints for admin filtering and Stage C.

Admin domain filter dropdowns are populated from distinct ``module.domain`` values
already stored on modules (`GET /admin/modules/domains`) — not from this file.

``MODULE_DOMAIN_CATALOG`` is a non-exhaustive set of common program-domain labels
used as LLM guidance in Stage C. It is **not** an ingestion allowlist: candidates
may use any normalized snake_case label that best matches the source topic (e.g.
``dengue``, ``pneumonia`` when the manual has a dedicated chapter).
"""

from __future__ import annotations

import re

# Common program-domain labels for Stage C prompt hints and normalization examples.
# Aligns loosely with behavioural_gap registry domains and dashboard filters.
MODULE_DOMAIN_CATALOG: frozenset[str] = frozenset(
    {
        "anc",
        "clinical",
        "diabetes",
        "digital",
        "documentation",
        "family_planning",
        "hypertension",
        "imci",
        "immunisation",
        "malaria",
        "ncd",
        "neonatal",
        "nutrition",
        "pnc",
        "referral",
        "rmnch",
        "tb",
    }
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_module_domain_label(raw: str) -> str:
    """Normalize a domain label to lowercase snake_case."""
    cleaned = raw.strip().lower()
    cleaned = _NON_ALNUM_RE.sub("_", cleaned)
    return cleaned.strip("_")


def catalog_domain_label(raw: str | None) -> str | None:
    """Normalize a Stage C domain label; return None when empty after normalization."""
    if raw is None or not str(raw).strip():
        return None
    normalized = normalize_module_domain_label(str(raw))
    return normalized or None


def module_domain_catalog_for_prompt() -> str:
    """Sorted comma-separated common domain labels for LLM prompt hints."""
    return ", ".join(sorted(MODULE_DOMAIN_CATALOG))
