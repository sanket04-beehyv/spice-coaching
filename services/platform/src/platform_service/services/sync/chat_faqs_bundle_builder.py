"""Build chat FAQ sync bundles for device sync."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import ChatFaqItem, ChatFaqsSyncBundle
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.chat_faq_repository import ChatFaqRepository
from platform_service.db.repositories.module_repository import ModuleRepository


class ChatFaqsBundleBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
    ) -> ChatFaqsSyncBundle:
        repo = ChatFaqRepository(self._session)
        computed_at = await repo.max_computed_at(tenant_id=tenant_id)

        if computed_at is None:
            target_count = 5
            scan_limit = 25
            module_repo = ModuleRepository(self._session)
            modules = await module_repo.list_recent_published_modules_by_published_at(
                tenant_id=tenant_id,
                limit=scan_limit,
            )
            module_ids = [m.id for m in modules]
            questions = await module_repo.list_quiz_questions_for_module_ids(module_ids)
            by_module_id: dict[UUID, list] = defaultdict(list)
            for q in questions:
                if q.module_id is not None:
                    by_module_id[q.module_id].append(q)

            fallback_faqs: list[ChatFaqItem] = []
            for m in modules:
                qs = by_module_id.get(m.id)
                if not qs:
                    continue
                q0 = qs[0]
                fallback_faqs.append(
                    ChatFaqItem(
                        id=q0.id,
                        question=q0.question_localized,
                        occurrence_count=0,
                        rank=len(fallback_faqs) + 1,
                        last_seen_at=None,
                    )
                )
                if len(fallback_faqs) >= target_count:
                    break

            return ChatFaqsSyncBundle(
                faqs=fallback_faqs,
                computed_at=None,
                server_time_utc=datetime.now(UTC).isoformat(),
            )

        rows = await repo.list_updated_since(tenant_id=tenant_id, since=since)
        return ChatFaqsSyncBundle(
            faqs=[
                ChatFaqItem(
                    id=row.id,
                    question=row.question_localized,
                    occurrence_count=row.occurrence_count,
                    rank=row.rank,
                    last_seen_at=row.last_seen_at,
                )
                for row in rows
            ],
            computed_at=computed_at,
            server_time_utc=datetime.now(UTC).isoformat(),
        )
