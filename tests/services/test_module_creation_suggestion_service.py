"""Service/worker tests for module creation suggestions refresh."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.services.module_creation_suggestion_classifier import (
    SUGGESTION_KIND_PROPOSED_TOPIC,
    ClassifiedSuggestion,
)
from platform_service.services.module_creation_suggestion_service import (
    ModuleCreationSuggestionService,
)
from platform_service.services.unattributed_demand_aggregator import DedupedEvidence


@pytest.mark.asyncio
async def test_refresh_skips_write_when_no_evidence() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    service = ModuleCreationSuggestionService(
        session,
        client=MagicMock(),
        settings=MagicMock(
            module_creation_suggestions_llm_timeout_seconds=60.0,
            module_creation_suggestions_max_suggestions=20,
            module_creation_suggestions_max_evidence=80,
        ),
        ch_client=MagicMock(),
    )
    service._aggregator.fetch_for_day = AsyncMock(return_value=([], []))
    service._repo.replace_for_day = AsyncMock()

    count = await service.refresh_for_day(tenant_id=None, suggestion_date=date(2026, 7, 29))
    assert count == 0
    service._repo.replace_for_day.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_replaces_after_successful_classify() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    settings = MagicMock(
        module_creation_suggestions_llm_timeout_seconds=60.0,
        module_creation_suggestions_max_suggestions=20,
        module_creation_suggestions_max_evidence=80,
    )
    service = ModuleCreationSuggestionService(
        session,
        client=MagicMock(),
        settings=settings,
        ch_client=MagicMock(),
    )
    question = DedupedEvidence(
        source="digital_help",
        text="Danger signs?",
        normalized_text="danger signs?",
        occurrence_count=4,
        last_seen_at=datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
        sample_event_id="e1",
        sample_chw_id=9,
    )
    service._aggregator.fetch_for_day = AsyncMock(return_value=([question], []))
    service._load_drafts = AsyncMock(return_value=[])
    service._classifier.classify = AsyncMock(
        return_value=[
            ClassifiedSuggestion(
                suggestion_kind=SUGGESTION_KIND_PROPOSED_TOPIC,
                matched_module_id=None,
                proposed_topic="Danger signs",
                display_title="Danger signs",
                rationale="Frequent unattributed questions",
                question_keys=["danger signs?"],
                request_keys=[],
            )
        ]
    )
    service._repo.replace_for_day = AsyncMock(return_value=[])

    count = await service.refresh_for_day(tenant_id=uuid4(), suggestion_date=date(2026, 7, 29))
    assert count == 1
    service._repo.replace_for_day.assert_awaited_once()
    kwargs = service._repo.replace_for_day.await_args.kwargs
    assert kwargs["suggestion_date"] == date(2026, 7, 29)
    assert len(kwargs["rows"]) == 1
    assert kwargs["rows"][0].display_title == "Danger signs"
    assert kwargs["rows"][0].rank == 1
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_does_not_replace_when_llm_fails() -> None:
    session = MagicMock()
    session.commit = AsyncMock()
    settings = MagicMock(
        module_creation_suggestions_llm_timeout_seconds=60.0,
        module_creation_suggestions_max_suggestions=20,
        module_creation_suggestions_max_evidence=80,
    )
    service = ModuleCreationSuggestionService(
        session,
        client=MagicMock(),
        settings=settings,
        ch_client=MagicMock(),
    )
    service._aggregator.fetch_for_day = AsyncMock(
        return_value=[
            (
                [
                    DedupedEvidence(
                        source="digital_help",
                        text="Q",
                        normalized_text="q",
                        occurrence_count=1,
                        last_seen_at=None,
                        sample_event_id=None,
                        sample_chw_id=None,
                    )
                ],
                [],
            )
        ][0]
    )
    service._load_drafts = AsyncMock(return_value=[])
    service._classifier.classify = AsyncMock(side_effect=RuntimeError("ai-runtime error: boom"))
    service._repo.replace_for_day = AsyncMock()

    with pytest.raises(RuntimeError, match="ai-runtime error"):
        await service.refresh_for_day(tenant_id=None, suggestion_date=date(2026, 7, 29))

    service._repo.replace_for_day.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_worker_processes_scopes() -> None:
    from platform_service.workers.module_creation_suggestions_worker import (
        refresh_module_creation_suggestions_job,
    )

    service = MagicMock()
    service.list_scopes = AsyncMock(return_value=[None, uuid4()])
    service.refresh_for_day = AsyncMock(side_effect=[2, 0])

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "platform_service.workers.module_creation_suggestions_worker.SessionLocal",
            return_value=session_cm,
        ),
        patch(
            "platform_service.workers.module_creation_suggestions_worker.ModuleCreationSuggestionService",
            return_value=service,
        ),
    ):
        result = await refresh_module_creation_suggestions_job()

    assert result["scopes_updated"] == 2
    assert result["suggestions_written"] == 2
    assert service.refresh_for_day.await_count == 2
