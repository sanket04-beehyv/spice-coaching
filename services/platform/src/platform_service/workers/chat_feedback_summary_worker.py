"""Weekly worker: synthesize chat feedback summaries from telemetry events."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from mc_contracts.chat_feedback_summary import ChatFeedbackSummaryResponse

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.chat_feedback_summary_repository import ChatFeedbackSummaryRepository
from platform_service.deps import get_ai_client, get_clickhouse_client
from platform_service.services.chat_feedback_aggregator import ChatFeedbackAggregator
from platform_service.services.chat_feedback_summary_generator import ChatFeedbackSummaryGenerator

logger = logging.getLogger(__name__)


async def aggregate_chat_feedback_summary_job() -> dict[str, int]:
    """Query ClickHouse, synthesize feedback summaries, and persist per tenant."""
    computed_at = datetime.now(UTC)
    ch_client = get_clickhouse_client()
    ai_client = get_ai_client()
    aggregator = ChatFeedbackAggregator(ch_client)
    generator = ChatFeedbackSummaryGenerator(client=ai_client)

    summary = {"tenants_updated": 0, "tenants_skipped": 0}
    try:
        ch_tenant_ids = await aggregator.distinct_tenant_ids()

        async with SessionLocal() as session:
            repo = ChatFeedbackSummaryRepository(session)
            snapshot_tenant_ids = await repo.list_tenant_ids()
            tenant_ids = sorted(set(ch_tenant_ids) | set(snapshot_tenant_ids))

            for tenant_id in tenant_ids:
                try:
                    watermark = await repo.get_computed_at(tenant_id)
                    since_ts = aggregator.resolve_since_ts(watermark=watermark, now=computed_at)
                    batch = await aggregator.fetch_since(tenant_id, since_ts=since_ts)
                    if not batch.events:
                        summary["tenants_skipped"] += 1
                        continue

                    previous_payload = await repo.get_payload(tenant_id)
                    previous_summary: ChatFeedbackSummaryResponse | None = None
                    if previous_payload is not None:
                        previous_summary = ChatFeedbackSummaryResponse.model_validate(previous_payload)

                    result = await generator.synthesize(
                        batch=batch,
                        period_start=watermark,
                        period_end=computed_at,
                        previous_summary=previous_summary,
                    )
                    await repo.upsert(
                        tenant_id=tenant_id,
                        payload_json=result.model_dump(mode="json"),
                        generated_at=result.generated_at,
                        computed_at=computed_at,
                    )
                    summary["tenants_updated"] += 1
                except Exception:
                    logger.exception("Chat feedback summary worker failed for tenant %s", tenant_id)
                    await session.rollback()
                    continue

            await session.commit()
    finally:
        ch_client.close()

    logger.info(
        "Chat feedback summary aggregation complete: updated=%d skipped=%d",
        summary["tenants_updated"],
        summary["tenants_skipped"],
    )
    return summary
