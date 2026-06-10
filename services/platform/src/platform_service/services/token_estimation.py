"""Rough token estimates for LLM budget decisions."""


def estimate_token_count(text: str) -> int:
    """Coarse token estimate using ~4 chars per token."""
    if not text:
        return 0
    return max(1, len(text) // 4)
