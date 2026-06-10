"""Tests for ai-runtime embedding-dimension alignment."""

import logging

import pytest
from ai_runtime.services.embedding_vector import align_embedding_dimension


def test_align_noop_when_matching() -> None:
    vec = [0.1] * 768
    assert align_embedding_dimension(vec, expected_dim=768) is vec


def test_align_truncates_when_longer_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    vec = [float(i) for i in range(1536)]
    with caplog.at_level(logging.WARNING, logger="ai_runtime.services.embedding_vector"):
        out = align_embedding_dimension(vec, expected_dim=768)
    assert len(out) == 768
    assert out == vec[:768]
    assert any("Truncating embedding" in rec.message for rec in caplog.records)


def test_align_raises_when_shorter() -> None:
    with pytest.raises(ValueError, match="smaller than configured corpus dimension"):
        align_embedding_dimension([0.1, 0.2], expected_dim=768)


def test_align_zero_length_raises() -> None:
    with pytest.raises(ValueError):
        align_embedding_dimension([], expected_dim=768)
