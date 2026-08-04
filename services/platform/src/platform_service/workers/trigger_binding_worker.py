"""Post-publish assessment-due trigger binding worker."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from mc_contracts.errors import ErrorCode

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.assessment_topic_classifier import AssessmentTopicClassifier
from platform_service.services.post_publish_step import finish_post_publish_step

logger = logging.getLogger(__name__)


def _merge_trigger_binding_flags(
    existing: dict[str, Any] | None,
    *,
    bound_topics: list[str],
    primary_topic: str | None,
    trigger_codes: list[str],
    rationale: str,
    source: str,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing) if existing else {}
    out["trigger_binding"] = {
        "bound_topics": bound_topics,
        "primary_topic": primary_topic,
        "trigger_codes": trigger_codes,
        "rationale": rationale,
        "source": source,
    }
    return out


def _enqueue_embedding(module_id: UUID, embedding_step_id: UUID | None) -> None:
    if embedding_step_id is None:
        return
    from platform_service.celery_tasks import generate_module_embedding_task

    generate_module_embedding_task.delay(str(module_id), str(embedding_step_id))


async def bind_assessment_triggers_for_module(
    module_id: UUID,
    *,
    step_id: UUID | None = None,
    embedding_step_id: UUID | None = None,
) -> int:
    """Classify module topics and bind module to assessment-due triggers."""
    settings = get_settings()
    bindings_written = 0
    result = None
    try:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                logger.warning("Trigger binding worker: module %s not found", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error_code=ErrorCode.MODULE_NOT_FOUND.value,
                    error_message=f"module {module_id} not found",
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                _enqueue_embedding(module_id, embedding_step_id)
                return 0

            classifier = AssessmentTopicClassifier(session)
            result = await classifier.classify_module(module)
            if not result.assessment_topics:
                module.quality_flags_jsonb = _merge_trigger_binding_flags(
                    module.quality_flags_jsonb,
                    bound_topics=[],
                    primary_topic=None,
                    trigger_codes=[],
                    rationale=result.rationale,
                    source=result.source,
                )
                await session.commit()
                await finish_post_publish_step(
                    step_id=step_id,
                    success=True,
                    output_summary={
                        "bindings_written": 0,
                        "skipped": "no_assessment_topics",
                        "source": result.source,
                    },
                )
                _enqueue_embedding(module_id, embedding_step_id)
                return 0

            trigger_repo = TriggerRepository(session)
            binding_specs: list[tuple[UUID, str, int]] = []
            trigger_codes: list[str] = []
            primary = result.primary_topic or result.assessment_topics[0]
            for topic in result.assessment_topics:
                trigger = await trigger_repo.get_assessment_due_trigger(topic)
                if trigger is None:
                    logger.warning(
                        "Trigger binding worker: no trigger seeded for topic %s (module %s)",
                        topic,
                        module_id,
                    )
                    continue
                relationship = "primary" if topic == primary else "secondary"
                weight = (
                    settings.trigger_binding_primary_weight
                    if relationship == "primary"
                    else settings.trigger_binding_secondary_weight
                )
                binding_specs.append((trigger.id, relationship, weight))
                trigger_codes.append(trigger.trigger_code)

            created = await trigger_repo.replace_assessment_due_bindings_for_module(
                module.id,
                bindings=binding_specs,
            )
            bindings_written = len(created)
            module.quality_flags_jsonb = _merge_trigger_binding_flags(
                module.quality_flags_jsonb,
                bound_topics=result.assessment_topics,
                primary_topic=primary,
                trigger_codes=trigger_codes,
                rationale=result.rationale,
                source=result.source,
            )
            await session.commit()
            logger.info(
                "Trigger binding worker: module %s bound %d trigger(s): %s",
                module_id,
                bindings_written,
                trigger_codes,
            )

        await finish_post_publish_step(
            step_id=step_id,
            success=True,
            output_summary={
                "bindings_written": bindings_written,
                "bound_topics": result.assessment_topics,
                "primary_topic": primary,
                "trigger_codes": trigger_codes,
                "source": result.source,
            },
        )
        _enqueue_embedding(module_id, embedding_step_id)
        return bindings_written
    except Exception as exc:
        logger.exception("Trigger binding worker: unhandled error for module %s", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=False,
            error_code=ErrorCode.TRIGGER_BINDING_FAILED.value,
            error_message=str(exc)[:500],
            error={"type": type(exc).__name__, "message": str(exc)[:500]},
        )
        _enqueue_embedding(module_id, embedding_step_id)
        raise


__all__ = ["bind_assessment_triggers_for_module"]
