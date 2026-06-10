"""Tests for the platform-side ``assert_embedding_dimension`` helper."""

import pytest
from platform_service.exceptions import EmbeddingDimensionError
from platform_service.services.embedding_vector import assert_embedding_dimension


def test_assert_returns_vector_when_dimension_matches() -> None:
    vec = [0.1] * 768
    assert assert_embedding_dimension(vec, expected_dim=768) is vec


def test_assert_raises_when_dimension_is_too_large() -> None:
    """ai-runtime should have truncated — if the platform sees a longer
    vector that means alignment never ran. Raise so the misconfiguration
    surfaces clearly rather than being silently re-truncated."""
    vec = [0.1] * 1536
    with pytest.raises(EmbeddingDimensionError, match="does not match corpus dimension 768"):
        assert_embedding_dimension(vec, expected_dim=768)


def test_assert_raises_when_dimension_is_too_small() -> None:
    with pytest.raises(EmbeddingDimensionError):
        assert_embedding_dimension([0.1, 0.2], expected_dim=768)


def test_assert_zero_length_raises() -> None:
    with pytest.raises(EmbeddingDimensionError):
        assert_embedding_dimension([], expected_dim=768)
