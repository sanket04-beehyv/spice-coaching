"""Build the configured ``VectorStore`` for a request / worker session."""

from __future__ import annotations

from mc_foundation.vectorstore import VectorStore
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.vectorstore.pgvector_store import PgVectorStore


def get_vector_store(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> VectorStore:
    """Return the vector backend selected by ``VECTOR_STORE_BACKEND``.

    Only ``pgvector`` is implemented today. Unknown values fail fast so a
    misconfigured deploy cannot silently fall back to an unintended store.
    """
    cfg = settings if settings is not None else get_settings()
    backend = cfg.vector_store_backend
    if backend == "pgvector":
        return PgVectorStore(session)
    raise ValueError(f"unsupported VECTOR_STORE_BACKEND={backend!r}; supported: 'pgvector'")
