"""Factory tests that do not require a database session."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from platform_service.config import Settings
from platform_service.vectorstore import PgVectorStore, get_vector_store


def test_get_vector_store_pgvector() -> None:
    session = MagicMock()
    store = get_vector_store(session, settings=Settings(vector_store_backend="pgvector"))
    assert isinstance(store, PgVectorStore)


def test_get_vector_store_unknown_backend() -> None:
    session = MagicMock()
    with pytest.raises(ValueError, match="unsupported VECTOR_STORE_BACKEND"):
        get_vector_store(session, settings=Settings(vector_store_backend="qdrant"))
