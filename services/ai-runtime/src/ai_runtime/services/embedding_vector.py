"""Canonical embedding-dimension alignment for ai-runtime.

This is the **only** place embedding vectors are truncated. Platform-side
helpers (``platform_service.services.embedding_vector.assert_embedding_dimension``)
raise on mismatch rather than re-truncating, so a misconfiguration
surfaces as a clear ``EmbeddingDimensionError`` instead of being silently
clipped twice.

Behaviour:
- vec length == expected: return unchanged.
- vec length > expected: WARN and truncate to ``expected_dim``.
- vec length < expected: raise ``ValueError`` (we cannot synthesise the
  missing dimensions; this is always a configuration bug).
"""

import logging

logger = logging.getLogger(__name__)


def align_embedding_dimension(vec: list[float], *, expected_dim: int) -> list[float]:
    """Truncate ``vec`` to ``expected_dim`` (with warning) or raise on shorter input."""
    if len(vec) == expected_dim:
        return vec
    if len(vec) > expected_dim:
        logger.warning(
            "Truncating embedding from %d to %d dimensions",
            len(vec),
            expected_dim,
        )
        return vec[:expected_dim]
    raise ValueError(
        f"embedding dimension {len(vec)} is smaller than configured corpus dimension {expected_dim}"
    )
