"""Fixed English title/description catalog for ingest progress tree nodes."""

from __future__ import annotations

from platform_service.services.run_state_service import (
    STAGE_CARD_DRAFT,
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    STAGE_CROSS_SOURCE_FUSION,
    STAGE_EMBEDDING_GENERATION,
    STAGE_EXTRACT,
    STAGE_GAP_CLASSIFICATION,
    STAGE_MODULE_IDENTIFY,
    STAGE_QUIZ_GENERATION,
    STAGE_SEARCH_METADATA_GENERATION,
    STAGE_THUMBNAIL,
    STAGE_TRIGGER_BINDING,
)

# (title, description) keyed by stage, with optional activity variants as "stage:activity".
_CATALOG: dict[str, tuple[str, str]] = {
    STAGE_THUMBNAIL: (
        "Generating thumbnail",
        "Rendering a preview image from the source document for admin and sync views.",
    ),
    STAGE_EXTRACT: (
        "Extracting content",
        "Reading pages or media from the source and building the structured outline.",
    ),
    STAGE_MODULE_IDENTIFY: (
        "Identifying modules",
        "Proposing module candidates from the extracted outline and content blocks.",
    ),
    STAGE_CARD_DRAFT: (
        "Drafting module cards",
        "Generating training cards (and optionally merging into an existing published module).",
    ),
    f"{STAGE_CARD_DRAFT}:published_module_merge": (
        "Merging into published module",
        "Comparing the candidate against existing modules and writing dual-path review_pending drafts.",
    ),
    STAGE_QUIZ_GENERATION: (
        "Generating quiz",
        "Creating assessment questions for the drafted module.",
    ),
    STAGE_EMBEDDING_GENERATION: (
        "Generating embeddings",
        "Computing vector embeddings used for retrieval and search.",
    ),
    STAGE_SEARCH_METADATA_GENERATION: (
        "Generating search metadata",
        "Building module-level search metadata for coaching retrieval.",
    ),
    STAGE_CARD_SEARCH_METADATA_GENERATION: (
        "Generating card search metadata",
        "Building per-card search metadata for finer-grained retrieval.",
    ),
    STAGE_GAP_CLASSIFICATION: (
        "Classifying behavioural gaps",
        "Linking the module to behavioural gap codes used for coaching targeting.",
    ),
    STAGE_TRIGGER_BINDING: (
        "Binding triggers",
        "Attaching delivery triggers so the module can surface at the right moment.",
    ),
    STAGE_CROSS_SOURCE_FUSION: (
        "Fusing across sources",
        "Matching related candidates from multiple source documents into fused modules.",
    ),
    "candidate": (
        "Module candidate",
        "Downstream drafting and post-publish work for one proposed module.",
    ),
    "fusion": (
        "Cross-source fusion",
        "Batch-level fusion across all sources in this ingest.",
    ),
    "chunk": (
        "Identify chunk",
        "Module identification for one token-budgeted slice of the source corpus.",
    ),
}


def catalog_entry(key: str, *, activity: str | None = None) -> tuple[str, str]:
    """Return (title, description) for a stage key, preferring activity-specific copy."""
    if activity:
        variant = f"{key}:{activity}"
        if variant in _CATALOG:
            return _CATALOG[variant]
    if key in _CATALOG:
        return _CATALOG[key]
    human = key.replace("_", " ").strip().capitalize() or "Pipeline step"
    return human, f"Pipeline stage `{key}` is in progress."


def candidate_catalog_entry(proposed_title: str) -> tuple[str, str]:
    title, _ = catalog_entry("candidate")
    description = (
        f"Drafting and post-publish stages for proposed module “{proposed_title}”."
        if proposed_title
        else catalog_entry("candidate")[1]
    )
    return title, description


def chunk_catalog_entry(chunk_id: str) -> tuple[str, str]:
    title, _ = catalog_entry("chunk")
    return f"{title} ({chunk_id})", catalog_entry("chunk")[1]
