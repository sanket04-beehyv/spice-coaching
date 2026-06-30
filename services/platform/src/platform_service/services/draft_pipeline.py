"""Shared draft / validate / publish steps for Stage D and fusion drafting."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.celery_tasks import (
    bind_assessment_triggers_task,
    classify_module_gaps_task,
    generate_module_card_search_metadata_batch_task,
    generate_module_embedding_task,
    generate_module_quiz_task,
    generate_module_search_metadata_task,
)
from platform_service.config import get_settings
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.source_page import SourcePage
from platform_service.services.card_drafter import (
    CardDrafter,
    CardDrafterError,
    CardDrafterResult,
)
from platform_service.services.module_card_validator import (
    ModuleCardValidator,
    annotate_field_flags,
)
from platform_service.services.post_publish import should_generate_quiz_for_sources
from platform_service.services.run_state_service import (
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    STAGE_EMBEDDING_GENERATION,
    STAGE_GAP_CLASSIFICATION,
    STAGE_QUIZ_GENERATION,
    STAGE_SEARCH_METADATA_GENERATION,
    STAGE_TRIGGER_BINDING,
    RunStateService,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardDraftOutcome:
    """Result of drafting and validating cards for one candidate."""

    cards: list[dict[str, Any]] | None
    cards_count: int
    insufficient_reason: str | None


class DraftPipeline:
    """Draft, validate, and post-publish steps shared by Stage D and fusion."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        card_drafter: CardDrafter | None = None,
    ) -> None:
        self._session = session
        self._card_drafter = card_drafter or CardDrafter()

    async def draft_and_validate_cards(
        self,
        *,
        candidate_id: UUID,
        candidate_dict: dict[str, Any],
        cited_blocks: list[dict[str, Any]],
        valid_block_ids: set[UUID],
    ) -> CardDraftOutcome:
        """Run card drafter, validate output, and enforce ``card_min_count``."""
        if not cited_blocks:
            return CardDraftOutcome(
                cards=None,
                cards_count=0,
                insufficient_reason="no_cited_blocks_resolvable",
            )

        try:
            card_result: CardDrafterResult = await self._card_drafter.draft(
                candidate=candidate_dict,
                cited_blocks=cited_blocks,
                valid_block_ids=valid_block_ids,
            )
        except CardDrafterError as exc:
            logger.exception("Card drafter failed for candidate %s", candidate_id)
            raise exc

        if card_result.insufficient_reason:
            return CardDraftOutcome(
                cards=None,
                cards_count=0,
                insufficient_reason=card_result.insufficient_reason,
            )

        cards = self.validate_cards(card_result.cards, candidate_id=candidate_id)
        settings = get_settings()
        if len(cards) < settings.card_min_count:
            return CardDraftOutcome(
                cards=None,
                cards_count=0,
                insufficient_reason="validator_dropped_too_many_cards",
            )

        return CardDraftOutcome(
            cards=cards,
            cards_count=len(cards),
            insufficient_reason=None,
        )

    def validate_cards(
        self,
        cards: list[dict[str, Any]],
        *,
        candidate_id: UUID,
    ) -> list[dict[str, Any]]:
        validator = ModuleCardValidator()
        validated: list[dict[str, Any]] = []
        for card in cards:
            res = validator.validate_card(card)
            if not res.is_valid:
                logger.warning(
                    "Draft pipeline dropping card with hard violations cand=%s violations=%s",
                    candidate_id,
                    res.hard_violations,
                )
                continue
            if res.soft_warnings:
                card["field_flags"] = annotate_field_flags(card.get("field_flags"), card_result=res)
            validated.append(card)
        return validated

    async def enqueue_post_publish(
        self,
        module_id: UUID,
        source_document_ids: list[UUID],
        *,
        ingestion_run_id: UUID,
        candidate_id: UUID,
    ) -> None:
        """Enqueue post-publish embedding and (when allowed) quiz workers.

        Creates ``ingestion_run_step`` rows so ingest polling can track
        quiz/embedding progress. Imported lazily so orchestrator unit tests
        that mock the session don't need a real Redis broker.
        """
        run_state = RunStateService(self._session)
        input_summary = {
            "candidate_id": str(candidate_id),
            "module_id": str(module_id),
        }
        embedding_step = await run_state.start_step(
            run_id=ingestion_run_id,
            stage=STAGE_EMBEDDING_GENERATION,
            input_summary=input_summary,
        )
        card_metadata_step_id: UUID | None = None
        metadata_step_id: UUID | None = None
        settings = get_settings()
        if settings.post_publish_search_metadata_enabled:
            if settings.post_publish_card_search_metadata_enabled:
                card_metadata_step = await run_state.start_step(
                    run_id=ingestion_run_id,
                    stage=STAGE_CARD_SEARCH_METADATA_GENERATION,
                    input_summary=input_summary,
                )
                card_metadata_step_id = card_metadata_step.id
            metadata_step = await run_state.start_step(
                run_id=ingestion_run_id,
                stage=STAGE_SEARCH_METADATA_GENERATION,
                input_summary=input_summary,
            )
            metadata_step_id = metadata_step.id
        gap_step_id: UUID | None = None
        if get_settings().post_publish_gap_classification_enabled:
            gap_step = await run_state.start_step(
                run_id=ingestion_run_id,
                stage=STAGE_GAP_CLASSIFICATION,
                input_summary=input_summary,
            )
            gap_step_id = gap_step.id
        trigger_binding_step_id: UUID | None = None
        if get_settings().post_publish_trigger_binding_enabled:
            trigger_binding_step = await run_state.start_step(
                run_id=ingestion_run_id,
                stage=STAGE_TRIGGER_BINDING,
                input_summary=input_summary,
            )
            trigger_binding_step_id = trigger_binding_step.id
        quiz_step_id: UUID | None = None
        if await should_generate_quiz_for_sources(self._session, source_document_ids):
            quiz_step = await run_state.start_step(
                run_id=ingestion_run_id,
                stage=STAGE_QUIZ_GENERATION,
                input_summary=input_summary,
            )
            quiz_step_id = quiz_step.id
        else:
            await run_state.skip_step(
                run_id=ingestion_run_id,
                stage=STAGE_QUIZ_GENERATION,
                reason="assessment_mode_read_only",
                input_summary=input_summary,
            )
            logger.info(
                "Draft pipeline: skipping quiz generation for module %s "
                "(all source documents have assessment_mode=read_only)",
                module_id,
            )

        await self._session.commit()

        try:
            if quiz_step_id is not None:
                generate_module_quiz_task.delay(str(module_id), str(quiz_step_id))
            if metadata_step_id is not None:
                if card_metadata_step_id is not None:
                    generate_module_card_search_metadata_batch_task.delay(
                        str(module_id),
                        str(card_metadata_step_id),
                        str(metadata_step_id),
                        str(embedding_step.id),
                        str(trigger_binding_step_id) if trigger_binding_step_id else None,
                    )
                else:
                    generate_module_search_metadata_task.delay(
                        str(module_id),
                        str(metadata_step_id),
                        str(embedding_step.id),
                        str(trigger_binding_step_id) if trigger_binding_step_id else None,
                    )
            elif trigger_binding_step_id is not None:
                bind_assessment_triggers_task.delay(
                    str(module_id),
                    str(trigger_binding_step_id),
                    str(embedding_step.id),
                )
            else:
                generate_module_embedding_task.delay(str(module_id), str(embedding_step.id))
            if gap_step_id is not None:
                classify_module_gaps_task.delay(str(module_id), str(gap_step_id))
        except Exception:
            logger.exception(
                "Draft pipeline: failed to enqueue post-publish jobs for module %s "
                "(module is still persisted; retry from dashboard)",
                module_id,
            )
            if quiz_step_id is not None:
                await run_state.fail_step(
                    quiz_step_id,
                    error={"type": "EnqueueError", "message": "failed to enqueue quiz Celery task"},
                )
            if metadata_step_id is not None:
                await run_state.fail_step(
                    metadata_step_id,
                    error={
                        "type": "EnqueueError",
                        "message": "failed to enqueue search metadata Celery task",
                    },
                )
            if card_metadata_step_id is not None:
                await run_state.fail_step(
                    card_metadata_step_id,
                    error={
                        "type": "EnqueueError",
                        "message": "failed to enqueue card search metadata Celery task",
                    },
                )
            await run_state.fail_step(
                embedding_step.id,
                error={"type": "EnqueueError", "message": "failed to enqueue embedding Celery task"},
            )
            if gap_step_id is not None:
                await run_state.fail_step(
                    gap_step_id,
                    error={
                        "type": "EnqueueError",
                        "message": "failed to enqueue gap classification Celery task",
                    },
                )
            if trigger_binding_step_id is not None:
                await run_state.fail_step(
                    trigger_binding_step_id,
                    error={
                        "type": "EnqueueError",
                        "message": "failed to enqueue trigger binding Celery task",
                    },
                )
            await self._session.commit()
            await run_state.maybe_finalize_ingestion_run(ingestion_run_id)

    async def load_cited_blocks(
        self,
        candidate: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[UUID]]:
        """Load every content_block referenced in the candidate's source_provenance."""
        block_ids: list[UUID] = []
        for entry in candidate.get("source_provenance", []) or []:
            for raw in entry.get("content_block_ids", []) or []:
                try:
                    block_ids.append(UUID(str(raw)))
                except ValueError:
                    continue
        if not block_ids:
            return [], set()
        result = await self._session.execute(
            select(ContentBlock, SourcePage.source_document_id)
            .join(SourcePage, ContentBlock.source_page_id == SourcePage.id)
            .where(ContentBlock.id.in_(block_ids))
        )
        rows = list(result.all())
        cited = [
            {
                "content_block_id": str(b.id),
                "source_document_id": str(sd_id),
                "block_type": b.block_type,
                "content_text": b.content_text,
                "content_language": b.content_language,
            }
            for b, sd_id in rows
        ]
        return cited, {b.id for b, _ in rows}

    @staticmethod
    def extract_source_document_ids(candidate: dict[str, Any]) -> list[UUID]:
        ids: set[UUID] = set()
        for entry in candidate.get("source_provenance", []) or []:
            try:
                ids.add(UUID(str(entry["source_document_id"])))
            except (KeyError, ValueError):
                continue
        return sorted(ids)

    @staticmethod
    def block_ids_from_cards(cards: list[dict[str, Any]]) -> set[UUID]:
        ids: set[UUID] = set()
        for card in cards:
            for raw in card.get("source_block_ids", []) or []:
                try:
                    ids.add(UUID(str(raw)))
                except (TypeError, ValueError):
                    continue
        return ids
