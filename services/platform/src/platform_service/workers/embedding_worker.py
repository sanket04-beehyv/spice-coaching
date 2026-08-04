"""Post-publish embedding worker.

Per `docs/ARCHITECTURE_RESET.md`. Triggered on module publish (Stage 3
enqueues a Celery task per `module_id`). Reads card text from ``module_card`` rows,
calls ai-runtime `/embed`, and writes the vector via the configured
``VectorStore`` (pgvector adapter persists to ``module.embedding`` today).

The embedding is per-module (not per-card). It serves admin/web semantic
search inside this repo; the Android repo's runtime grounding flow uses it
via a standard fetch endpoint (added in step 11). Failure does not block
the module — the module remains readable in the dashboard via title and
full-text search until a later embedding run succeeds.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from mc_contracts.errors import ErrorCode
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.deps import get_ai_client
from platform_service.exceptions import EmbeddingDimensionError
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.embedding_vector import assert_embedding_dimension
from platform_service.services.module_search_text import module_text_for_search
from platform_service.services.post_publish_step import finish_post_publish_step
from platform_service.vectorstore import MODULES_COLLECTION, get_vector_store

logger = logging.getLogger(__name__)


def _module_text_for_embedding(module: Module, cards: list[dict[str, Any]]) -> str:
    """Backward-compatible alias for :func:`module_text_for_search`."""
    return module_text_for_search(module, cards=cards)


async def generate_embedding_for_module(module_id: UUID, *, step_id: UUID | None = None) -> bool:
    """Generate and persist a per-module embedding. Returns True on success.

    Persistence goes through the configured ``VectorStore`` (pgvector adapter
    writes ``module.embedding`` today).
    """
    try:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                logger.warning("Embedding worker: module %s not found", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.MODULE_NOT_FOUND.value,
                    error_message=f"module {module_id} not found",
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                return False
            card_rows = await ModuleReadRepository(session).list_cards(module_id)
            cards = [card_row_to_dict(row) for row in card_rows]
            text_input = _module_text_for_embedding(module, cards)
            if not text_input.strip():
                logger.info("Embedding worker: module %s has no text; skipping", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.MODULE_HAS_NO_TEXT.value,
                    error_message="module has no embeddable text",
                    error={"type": "NoText", "message": "module has no embeddable text"},
                )
                return False

            client = get_ai_client()
            try:
                vectors = await client.embed([text_input])
            except Exception as exc:  # ai-runtime errors are not module-blocking
                logger.error("Embedding worker: ai-runtime error for module %s: %s", module_id, exc)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.EMBEDDING_FAILED.value,
                    error_message=str(exc)[:500],
                    error={"type": type(exc).__name__, "message": str(exc)[:500]},
                )
                return False
            if not vectors:
                logger.error("Embedding worker: empty embedding for module %s", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.EMPTY_EMBEDDING.value,
                    error_message="ai-runtime returned no vectors",
                    error={"type": "EmptyEmbedding", "message": "ai-runtime returned no vectors"},
                )
                return False

            settings = get_settings()
            try:
                aligned = assert_embedding_dimension(vectors[0], expected_dim=settings.embedding_dimension)
            except EmbeddingDimensionError as exc:
                logger.error("Embedding worker: dimension mismatch for module %s: %s", module_id, exc)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.EMBEDDING_DIMENSION_MISMATCH.value,
                    error_message=str(exc)[:500],
                    error={"type": "EmbeddingDimensionError", "message": str(exc)[:500]},
                )
                return False

            await _persist_embedding(session, module_id, aligned)
            await session.commit()
            logger.info("Embedding worker: module %s embedded", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=True,
            output_summary={"embedded": True},
        )
        return True
    except Exception as exc:
        logger.exception("Embedding worker: unhandled error for module %s", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=False,
            error_code=ErrorCode.EMBEDDING_FAILED.value,
            error_message=str(exc)[:500],
            error={"type": type(exc).__name__, "message": str(exc)[:500]},
        )
        raise


async def _persist_embedding(session: AsyncSession, module_id: UUID, vector: list[float]) -> None:
    """Persist the embedding via the configured ``VectorStore`` backend."""
    store = get_vector_store(session)
    await store.upsert(
        [
            {
                "collection": MODULES_COLLECTION,
                "id": str(module_id),
                "vector": list(vector),
            }
        ]
    )


__all__ = ["generate_embedding_for_module"]
