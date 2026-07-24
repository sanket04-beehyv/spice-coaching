"""Post-publish card search metadata workers.

Generates locale-keyed lexical enrichment (primary locale only) for all module
cards in one LLM call, persists to ``module_card.search_metadata_jsonb``,
then chains module-level search metadata generation.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.db.repositories.module_write_repository import ModuleWriteRepository
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.card_search_metadata_generator import CardSearchMetadataGenerator
from platform_service.services.post_publish_step import finish_post_publish_step

logger = logging.getLogger(__name__)


def _enqueue_module_search_metadata(
    module_id: UUID,
    *,
    metadata_step_id: UUID | None,
    embedding_step_id: UUID | None,
    trigger_binding_step_id: UUID | None,
) -> None:
    from platform_service.celery_tasks import generate_module_search_metadata_task

    generate_module_search_metadata_task.delay(
        str(module_id),
        str(metadata_step_id) if metadata_step_id else None,
        str(embedding_step_id) if embedding_step_id else None,
        str(trigger_binding_step_id) if trigger_binding_step_id else None,
    )


def _build_output_summary(
    *,
    cards_total: int,
    succeeded_indices: list[int],
    failed_indices: list[int],
    skipped_indices: list[int],
) -> dict[str, Any]:
    return {
        "cards_total": cards_total,
        "cards_succeeded_indices": succeeded_indices,
        "cards_failed_indices": failed_indices,
        "cards_skipped_indices": skipped_indices,
    }


async def _complete_card_step(
    *,
    card_step_id: UUID | None,
    success: bool,
    output_summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    await finish_post_publish_step(
        step_id=card_step_id,
        success=success,
        output_summary=output_summary,
        error=error,
    )


async def generate_card_search_metadata_batch(
    module_id: UUID,
    *,
    card_step_id: UUID | None = None,
    metadata_step_id: UUID | None = None,
    embedding_step_id: UUID | None = None,
    trigger_binding_step_id: UUID | None = None,
    force: bool = False,
    chain_downstream: bool = True,
) -> int:
    """Generate search metadata for all cards in one LLM call, then chain module metadata."""

    def _chain_module_metadata() -> None:
        if chain_downstream:
            _enqueue_module_search_metadata(
                module_id,
                metadata_step_id=metadata_step_id,
                embedding_step_id=embedding_step_id,
                trigger_binding_step_id=trigger_binding_step_id,
            )

    try:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                logger.warning("Card search metadata batch: module %s not found", module_id)
                await _complete_card_step(
                    card_step_id=card_step_id,
                    success=False,
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                _chain_module_metadata()
                return 0

            card_rows = await ModuleReadRepository(session).list_cards(module_id)
            cards = [card_row_to_dict(row) for row in card_rows]
            card_indices = list(range(len(cards)))

            if not card_indices:
                logger.info(
                    "Card search metadata batch: module %s has no cards; skipping to module metadata",
                    module_id,
                )
                await _complete_card_step(
                    card_step_id=card_step_id,
                    success=True,
                    output_summary={"cards_total": 0, "cards_enqueued": 0},
                )
                _chain_module_metadata()
                return 0

            skipped_indices: list[int] = []
            to_generate: list[int] = []
            for card_index in card_indices:
                card = cards[card_index]
                if card.get("search_metadata") and not force:
                    skipped_indices.append(card_index)
                else:
                    to_generate.append(card_index)

            if not to_generate:
                output_summary = _build_output_summary(
                    cards_total=len(card_indices),
                    succeeded_indices=[],
                    failed_indices=[],
                    skipped_indices=skipped_indices,
                )
                logger.info(
                    "Card search metadata batch: module %s all cards already have metadata",
                    module_id,
                )
                await _complete_card_step(
                    card_step_id=card_step_id,
                    success=True,
                    output_summary=output_summary,
                )
                _chain_module_metadata()
                return 0

            generator = CardSearchMetadataGenerator()
            result = await generator.generate_for_module(module, to_generate, cards=cards)

            succeeded_indices = sorted(result.metadata_by_index)
            failed_indices = sorted(result.failed_indices)

            if result.error and not succeeded_indices:
                logger.warning(
                    "Card search metadata batch: generation failed module=%s: %s",
                    module_id,
                    result.error,
                )
                output_summary = _build_output_summary(
                    cards_total=len(card_indices),
                    succeeded_indices=[],
                    failed_indices=to_generate,
                    skipped_indices=skipped_indices,
                )
                await _complete_card_step(
                    card_step_id=card_step_id,
                    success=False,
                    output_summary=output_summary,
                    error={"type": "GenerationFailed", "message": result.error},
                )
                _chain_module_metadata()
                return 0

            if result.metadata_by_index:
                repo = ModuleWriteRepository()
                repo._session = session
                await repo.patch_cards_search_metadata(module_id, result.metadata_by_index)
                await session.commit()
                logger.info(
                    "Card search metadata batch: module %s persisted metadata for %s card(s)",
                    module_id,
                    len(result.metadata_by_index),
                )

            output_summary = _build_output_summary(
                cards_total=len(card_indices),
                succeeded_indices=succeeded_indices,
                failed_indices=failed_indices,
                skipped_indices=skipped_indices,
            )
            step_success = bool(succeeded_indices) or bool(skipped_indices)
            await _complete_card_step(
                card_step_id=card_step_id,
                success=step_success,
                output_summary=output_summary,
                error=(
                    {"type": "PartialFailure", "message": result.error}
                    if result.error and failed_indices
                    else None
                ),
            )
            _chain_module_metadata()
            return len(to_generate)
    except Exception:
        logger.exception(
            "Card search metadata batch: unhandled error module=%s",
            module_id,
        )
        raise


__all__ = [
    "generate_card_search_metadata_batch",
]
