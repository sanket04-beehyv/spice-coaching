"""Post-publish search metadata worker.

Generates bilingual lexical enrichment for module retrieval via LLM, persists
to ``module.search_metadata_jsonb``, then chains embedding generation so
vectors include the enriched text.
"""

from __future__ import annotations

import logging
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.services.module_search_metadata_generator import ModuleSearchMetadataGenerator
from platform_service.services.post_publish_step import finish_post_publish_step

logger = logging.getLogger(__name__)


def _enqueue_embedding(module_id: UUID, embedding_step_id: UUID | None) -> None:
    from platform_service.celery_tasks import generate_module_embedding_task

    step_arg = str(embedding_step_id) if embedding_step_id else None
    generate_module_embedding_task.delay(str(module_id), step_arg)


def _enqueue_post_metadata(
    module_id: UUID, *, embedding_step_id: UUID | None, trigger_binding_step_id: UUID | None
) -> None:
    if trigger_binding_step_id is not None:
        from platform_service.celery_tasks import bind_assessment_triggers_task

        bind_assessment_triggers_task.delay(
            str(module_id),
            str(trigger_binding_step_id),
            str(embedding_step_id) if embedding_step_id else None,
        )
        return
    _enqueue_embedding(module_id, embedding_step_id)


async def generate_search_metadata_for_module(
    module_id: UUID,
    *,
    step_id: UUID | None = None,
    embedding_step_id: UUID | None = None,
    trigger_binding_step_id: UUID | None = None,
    chain_downstream: bool = True,
) -> bool:
    """Generate and persist search metadata. Chains embedding afterward when enabled."""
    metadata_written = False

    def _chain_post_metadata() -> None:
        if chain_downstream:
            _enqueue_post_metadata(
                module_id,
                embedding_step_id=embedding_step_id,
                trigger_binding_step_id=trigger_binding_step_id,
            )

    try:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                logger.warning("Search metadata worker: module %s not found", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                _chain_post_metadata()
                return False

            generator = ModuleSearchMetadataGenerator()
            result = await generator.generate(module)
            if result.metadata is None:
                logger.warning(
                    "Search metadata worker: generation failed for module %s: %s",
                    module_id,
                    result.error,
                )
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error={
                        "type": "GenerationError",
                        "message": (result.error or "unknown")[:500],
                    },
                )
                _chain_post_metadata()
                return False

            module.search_metadata_jsonb = result.metadata
            await session.commit()
            metadata_written = True
            logger.info("Search metadata worker: module %s metadata persisted", module_id)

        await finish_post_publish_step(
            step_id=step_id,
            success=True,
            output_summary={"metadata_written": True},
        )
        _chain_post_metadata()
        return metadata_written
    except Exception as exc:
        logger.exception("Search metadata worker: unhandled error for module %s", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=False,
            error={"type": type(exc).__name__, "message": str(exc)[:500]},
        )
        _chain_post_metadata()
        raise


__all__ = ["generate_search_metadata_for_module"]
