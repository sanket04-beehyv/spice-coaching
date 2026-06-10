"""Domain errors raised by platform service layers.

Kept narrow on purpose: each entry is something a handler may want to
catch and map to a specific HTTP response. Wider RuntimeErrors should
not live here — they go in the module that raises them.
"""


class PlatformError(Exception):
    """Base for platform domain errors."""


class EmbeddingDimensionError(PlatformError):
    """Embedding vector length does not match the configured corpus dimension.

    Always a configuration bug — the canonical truncation point in
    ai-runtime should have aligned the vector before it crossed the
    boundary. Platform helpers assert against the corpus dimension and
    raise this so the mismatch surfaces clearly instead of being
    re-truncated.
    """
