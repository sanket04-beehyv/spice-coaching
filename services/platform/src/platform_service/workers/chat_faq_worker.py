"""Weekly worker: cluster chat questions and synthesize bilingual FAQs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.chat_faq_repository import ChatFaqRepository, ChatFaqRow
from platform_service.deps import get_ai_client, get_clickhouse_client
from platform_service.services.chat_faq_aggregator import ChatFaqAggregator
from platform_service.services.chat_faq_clusterer import ChatFaqClusterer
from platform_service.services.chat_faq_generator import ChatFaqGenerator

logger = logging.getLogger(__name__)


async def aggregate_chat_faqs_job() -> dict[str, int]:
    """Query ClickHouse, cluster questions, synthesize FAQs, and persist per tenant."""
    computed_at = datetime.now(UTC)
    ch_client = get_clickhouse_client()
    ai_client = get_ai_client()
    try:
        tenant_candidates = await ChatFaqAggregator(ch_client).fetch_candidates()
    finally:
        ch_client.close()

    clusterer = ChatFaqClusterer(ai_client)
    generator = ChatFaqGenerator(client=ai_client)

    summary = {"tenants_updated": 0, "faqs_written": 0}
    async with SessionLocal() as session:
        repo = ChatFaqRepository(session)
        for batch in tenant_candidates:
            tenant_id: UUID = batch.tenant_id
            try:
                clusters = await clusterer.cluster(batch.questions)
                synthesized = await generator.synthesize(tenant_id, clusters)
                rows = [
                    ChatFaqRow(
                        id=faq.id,
                        question_localized=faq.question_localized,
                        normalized_question=faq.normalized_question,
                        occurrence_count=faq.occurrence_count,
                        rank=faq.rank,
                        last_seen_at=faq.last_seen_at,
                    )
                    for faq in synthesized
                ]
                await repo.replace_tenant_faqs(tenant_id, rows, computed_at=computed_at)
                summary["tenants_updated"] += 1
                summary["faqs_written"] += len(rows)
            except Exception:
                logger.exception("Chat FAQ worker failed for tenant %s", tenant_id)
                await session.rollback()
                continue
        await session.commit()

    logger.info(
        "Chat FAQ aggregation complete: tenants=%d faqs=%d",
        summary["tenants_updated"],
        summary["faqs_written"],
    )
    return summary
