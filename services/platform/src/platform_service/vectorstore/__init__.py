"""Platform vector-store adapters (pgvector today; more backends later)."""

from platform_service.vectorstore.factory import get_vector_store
from platform_service.vectorstore.pgvector_store import MODULES_COLLECTION, PgVectorStore

__all__ = [
    "MODULES_COLLECTION",
    "PgVectorStore",
    "get_vector_store",
]
