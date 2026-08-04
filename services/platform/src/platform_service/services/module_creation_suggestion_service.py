"""Daily module-creation suggestions: refresh job + dashboard reads."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from mc_contracts.dashboard import (
    ModuleCreationSuggestionDetailResponse,
    ModuleCreationSuggestionEvidenceItem,
    ModuleCreationSuggestionListItem,
    ModuleCreationSuggestionListResponse,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_creation_suggestion import ModuleCreationSuggestion
from platform_service.db.repositories.module_creation_suggestion_repository import (
    EvidenceRow,
    ModuleCreationSuggestionRepository,
    SuggestionRow,
)
from platform_service.db.repositories.training_request_repository import TrainingRequestRepository
from platform_service.deps import get_ai_client, get_clickhouse_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.module_creation_suggestion_classifier import (
    ClassifiedSuggestion,
    DraftCatalogItem,
    ModuleCreationSuggestionClassifier,
)
from platform_service.services.unattributed_demand_aggregator import (
    DedupedEvidence,
    UnattributedDemandAggregator,
)

logger = logging.getLogger(__name__)

_SOURCE_DIGITAL_HELP = "digital_help"
_SOURCE_MODULE_REQUESTED = "module_requested"


def _display_title(title_localized: dict[str, str] | None, *, preferred: str = "en") -> str:
    if not title_localized:
        return "Untitled module"
    if preferred in title_localized and title_localized[preferred].strip():
        return title_localized[preferred].strip()
    for key in ("en", "bn"):
        value = title_localized.get(key)
        if value and value.strip():
            return value.strip()
    for value in title_localized.values():
        if value and str(value).strip():
            return str(value).strip()
    return "Untitled module"


class ModuleCreationSuggestionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        client: AIRuntimeClient | None = None,
        settings: Settings | None = None,
        ch_client: ClickHouseClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._client = client or get_ai_client()
        self._ch = ch_client if ch_client is not None else get_clickhouse_client()
        self._repo = ModuleCreationSuggestionRepository(session)
        self._requests = TrainingRequestRepository(session)
        self._aggregator = UnattributedDemandAggregator(self._ch)
        self._classifier = ModuleCreationSuggestionClassifier(
            session,
            client=self._client,
            settings=self._settings,
        )

    async def list_scopes(self) -> list[UUID | None]:
        """Scopes to process: global (None) plus each active tenant (training-request tenants)."""
        scopes: list[UUID | None] = [None]
        scopes.extend(await self._requests.distinct_tenant_ids())
        # Also include tenants that had unattributed evidence yesterday.
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        for tid in await self._aggregator.list_tenant_ids_for_day(event_date=yesterday):
            if tid not in scopes:
                scopes.append(tid)
        return scopes

    async def refresh_for_day(
        self,
        *,
        tenant_id: UUID | None,
        suggestion_date: date | None = None,
    ) -> int:
        """Classify and replace suggestions for one scope + UTC day. Returns suggestion count."""
        day = suggestion_date or (datetime.now(UTC).date() - timedelta(days=1))
        questions, requests = await self._aggregator.fetch_for_day(
            tenant_id=tenant_id,
            event_date=day,
        )
        if not questions and not requests:
            logger.info(
                "Module creation suggestions: no evidence tenant=%s date=%s",
                tenant_id,
                day,
            )
            return 0

        drafts = await self._load_drafts(tenant_id=tenant_id)
        timeout = float(self._settings.module_creation_suggestions_llm_timeout_seconds)
        classified = await asyncio.wait_for(
            self._classifier.classify(
                suggestion_date=day,
                drafts=drafts,
                questions=questions,
                requests=requests,
            ),
            timeout=timeout,
        )

        question_by_key = {q.normalized_text: q for q in questions}
        request_by_key = {r.normalized_text: r for r in requests}
        rows = self._to_suggestion_rows(
            classified,
            question_by_key=question_by_key,
            request_by_key=request_by_key,
        )
        computed_at = datetime.now(UTC)
        await self._repo.replace_for_day(
            tenant_id=tenant_id,
            suggestion_date=day,
            rows=rows,
            computed_at=computed_at,
        )
        await self._session.commit()
        logger.info(
            "Module creation suggestions refreshed tenant=%s date=%s count=%d",
            tenant_id,
            day,
            len(rows),
        )
        return len(rows)

    async def list_suggestions(
        self,
        *,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
        limit: int = 20,
        offset: int = 0,
    ) -> ModuleCreationSuggestionListResponse:
        rows, total = await self._repo.list_in_range(
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        total_pages = math.ceil(total / limit) if limit > 0 and total > 0 else 0
        return ModuleCreationSuggestionListResponse(
            from_date=from_date,
            to_date=to_date,
            suggestions=[self._to_list_item(r) for r in rows],
            total_suggestions=total,
            total_pages=total_pages,
            limit=limit,
            offset=offset,
        )

    async def get_detail(
        self,
        *,
        suggestion_id: UUID,
        tenant_id: UUID | None,
    ) -> ModuleCreationSuggestionDetailResponse:
        row = await self._repo.get_detail(suggestion_id=suggestion_id, tenant_id=tenant_id)
        if row is None:
            raise LookupError(f"suggestion not found: {suggestion_id}")
        questions: list[ModuleCreationSuggestionEvidenceItem] = []
        requests: list[ModuleCreationSuggestionEvidenceItem] = []
        for ev in sorted(
            row.evidence,
            key=lambda e: (-e.occurrence_count, e.normalized_text),
        ):
            item = ModuleCreationSuggestionEvidenceItem(
                source=ev.source,
                text=ev.text,
                occurrence_count=ev.occurrence_count,
                last_seen_at=ev.last_seen_at,
                sample_chw_id=ev.sample_chw_id,
            )
            if ev.source == _SOURCE_DIGITAL_HELP:
                questions.append(item)
            elif ev.source == _SOURCE_MODULE_REQUESTED:
                requests.append(item)
        return ModuleCreationSuggestionDetailResponse(
            suggestion=self._to_list_item(row),
            questions=questions,
            requests=requests,
        )

    async def _load_drafts(self, *, tenant_id: UUID | None) -> list[DraftCatalogItem]:
        stmt = select(Module).where(Module.lifecycle_status == "draft")
        if tenant_id is None:
            stmt = stmt.where(Module.tenant_id.is_(None))
        else:
            stmt = stmt.where(Module.tenant_id == tenant_id)
        modules = list((await self._session.execute(stmt)).scalars().all())
        return [
            DraftCatalogItem(
                module_id=m.id,
                title=_display_title(m.title_localized),
            )
            for m in modules
        ]

    def _to_suggestion_rows(
        self,
        classified: list[ClassifiedSuggestion],
        *,
        question_by_key: dict[str, DedupedEvidence],
        request_by_key: dict[str, DedupedEvidence],
    ) -> list[SuggestionRow]:
        scored: list[tuple[int, SuggestionRow]] = []
        for item in classified:
            evidence: list[EvidenceRow] = []
            q_count = 0
            r_count = 0
            for key in item.question_keys:
                ev = question_by_key.get(key)
                if ev is None:
                    continue
                q_count += ev.occurrence_count
                evidence.append(
                    EvidenceRow(
                        source=ev.source,
                        text=ev.text,
                        normalized_text=ev.normalized_text,
                        occurrence_count=ev.occurrence_count,
                        last_seen_at=ev.last_seen_at,
                        sample_event_id=ev.sample_event_id,
                        sample_chw_id=ev.sample_chw_id,
                    )
                )
            for key in item.request_keys:
                ev = request_by_key.get(key)
                if ev is None:
                    continue
                r_count += ev.occurrence_count
                evidence.append(
                    EvidenceRow(
                        source=ev.source,
                        text=ev.text,
                        normalized_text=ev.normalized_text,
                        occurrence_count=ev.occurrence_count,
                        last_seen_at=ev.last_seen_at,
                        sample_event_id=ev.sample_event_id,
                        sample_chw_id=ev.sample_chw_id,
                    )
                )
            if not evidence:
                continue
            evidence_count = q_count + r_count
            scored.append(
                (
                    evidence_count,
                    SuggestionRow(
                        suggestion_kind=item.suggestion_kind,
                        matched_module_id=item.matched_module_id,
                        proposed_topic=item.proposed_topic,
                        display_title=item.display_title,
                        rationale=item.rationale,
                        question_count=q_count,
                        request_count=r_count,
                        evidence_count=evidence_count,
                        rank=0,
                        evidence=evidence,
                    ),
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            SuggestionRow(
                suggestion_kind=row.suggestion_kind,
                matched_module_id=row.matched_module_id,
                proposed_topic=row.proposed_topic,
                display_title=row.display_title,
                rationale=row.rationale,
                question_count=row.question_count,
                request_count=row.request_count,
                evidence_count=row.evidence_count,
                rank=index + 1,
                evidence=row.evidence,
            )
            for index, (_score, row) in enumerate(scored)
        ]

    @staticmethod
    def _to_list_item(row: ModuleCreationSuggestion) -> ModuleCreationSuggestionListItem:
        return ModuleCreationSuggestionListItem(
            id=row.id,
            suggestion_date=row.suggestion_date,
            suggestion_kind=row.suggestion_kind,
            matched_module_id=row.matched_module_id,
            proposed_topic=row.proposed_topic,
            display_title=row.display_title,
            rationale=row.rationale,
            question_count=row.question_count,
            request_count=row.request_count,
            evidence_count=row.evidence_count,
            rank=row.rank,
            computed_at=row.computed_at,
        )
