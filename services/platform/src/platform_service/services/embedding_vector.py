"""Assert (never truncate) that an embedding matches the corpus dimension.

Canonical alignment lives in ai-runtime
(``ai_runtime.services.embedding_vector.align_embedding_dimension``).
Platform-side callers — the embedding worker (write side) and the RAG
query path (search side) — pass returned vectors through this assertion
helper instead of re-truncating, so a misconfigured provider surfaces
as ``EmbeddingDimensionError`` rather than as a silent double
truncation that no log line catches.
"""

from platform_service.exceptions import EmbeddingDimensionError


def assert_embedding_dimension(vec: list[float], *, expected_dim: int) -> list[float]:
    """Return ``vec`` if its length matches ``expected_dim``; raise otherwise."""
    if len(vec) != expected_dim:
        raise EmbeddingDimensionError(
            f"embedding dimension {len(vec)} does not match corpus dimension {expected_dim}; "
            "the ai-runtime align step did not produce the expected size — check ai-runtime settings"
        )
    return vec
