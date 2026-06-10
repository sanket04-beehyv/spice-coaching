"""Post-publish behavioural gap classification worker.

Maps a drafted module to 0..N secondary ``behavioural_gap`` links where
``domain = 'referral'`` via LLM, while preserving the per-module primary gap
created at Stage 2-draft.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.services.module_gap_classifier import ModuleGapClassifier
from platform_service.services.post_publish_step import finish_post_publish_step

logger = logging.getLogger(__name__)


def _merge_gap_classification_flags(
    existing: dict[str, Any] | None,
    *,
    gap_codes: list[str],
    rationale: str,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing) if existing else {}
    out["gap_classification"] = {
        "associated_gap_codes": gap_codes,
        "rationale": rationale,
    }
    return out


async def classify_module_gaps_for_module(module_id: UUID, *, step_id: UUID | None = None) -> int:
    """Classify module against referral-domain registry gaps. Returns secondary link count."""
    secondary_count = 0
    associated_codes: list[str] = []
    try:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                logger.warning("Gap classification worker: module %s not found", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                return 0

            if module.primary_gap_id is None:
                logger.info(
                    "Gap classification worker: module %s has no primary_gap_id; skipping",
                    module_id,
                )
                await finish_post_publish_step(
                    step_id=step_id,
                    success=True,
                    output_summary={"secondary_links_written": 0, "skipped": "no_primary_gap"},
                )
                return 0

            classifier = ModuleGapClassifier(session)
            result = await classifier.classify_module(module)
            associated_codes = result.associated_gap_codes

            gap_repo = ModuleGapRepository(session)
            await gap_repo.replace_secondary_links(
                module.id,
                secondary_gap_ids=result.associated_gap_ids,
                primary_gap_id=module.primary_gap_id,
            )
            module.quality_flags_jsonb = _merge_gap_classification_flags(
                module.quality_flags_jsonb,
                gap_codes=result.associated_gap_codes,
                rationale=result.rationale,
            )
            await session.commit()

            secondary_count = len(result.associated_gap_ids)
            logger.info(
                "Gap classification worker: module %s linked %d secondary gap(s): %s",
                module_id,
                secondary_count,
                result.associated_gap_codes,
            )

        await finish_post_publish_step(
            step_id=step_id,
            success=True,
            output_summary={
                "secondary_links_written": secondary_count,
                "associated_gap_codes": associated_codes,
            },
        )
        return secondary_count
    except Exception as exc:
        logger.exception("Gap classification worker: unhandled error for module %s", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=False,
            error={"type": type(exc).__name__, "message": str(exc)[:500]},
        )
        raise


__all__ = ["classify_module_gaps_for_module"]
