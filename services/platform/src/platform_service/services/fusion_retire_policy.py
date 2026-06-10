"""Retire-heuristic policy for cross-source fusion constituents."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class FusionRetirePolicy:
    """Find published modules whose source candidate was a constituent
    of a fusion group; mark them retired.

    Heuristic match: title_en == candidate.proposed_title AND
    candidate's source_document_id is in module.source_document_ids
    AND lifecycle_status = 'published'. No candidate→module FK exists
    in the schema today (modules know their source DOCS, not their
    source CANDIDATE); the heuristic is precise enough because every
    per-source candidate produces exactly one module with title_en
    == proposed_title (the drafter's persistence path uses the
    candidate's English title verbatim).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def retire_constituent_modules(
        self,
        constituent_ids: list[UUID],
        candidates_by_id: dict[str, dict[str, Any]],
    ) -> int:
        if not constituent_ids:
            return 0
        retired = 0
        for cid in constituent_ids:
            c = candidates_by_id.get(str(cid))
            if c is None:
                continue
            title = c.get("proposed_title", "")
            sd_id = c.get("source_document_id")
            if not title or not sd_id:
                continue
            result = await self._session.execute(
                text("""
                    UPDATE module
                    SET lifecycle_status = 'retired'
                    WHERE lifecycle_status = 'published'
                      AND title_en = :title
                      AND :sd_id = ANY(source_document_ids)
                    RETURNING id
                """),
                {"title": title, "sd_id": str(sd_id)},
            )
            n = len(list(result.scalars().all()))
            if n:
                logger.info(
                    "Stage 2b runner: retired %d constituent module(s) titled %r (source %s)",
                    n,
                    title,
                    sd_id[:8] if sd_id else "?",
                )
            retired += n
        return retired
