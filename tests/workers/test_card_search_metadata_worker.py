"""Tests for post-publish card search metadata workers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.ingestion_run import IngestionRunStep
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.card_search_metadata_generator import CardSearchMetadataBatchResult
from platform_service.services.run_state_service import (
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    RunStateService,
)
from platform_service.workers.card_search_metadata_worker import (
    generate_card_search_metadata_batch,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE ingestion_run_step, ingestion_run, module, module_family, "
            "source_document RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_module(session: AsyncSession, *, cards: list[dict] | None = None) -> Module:
    fam = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "Test module"},
        domain="rmnch",
        module_type="refresher",
        module_json={
            "cards": cards
            or [
                {"title": {"bn": "T1"}, "body": {"bn": "body1"}},
                {"title": {"bn": "T2"}, "body": {"bn": "body2"}},
            ]
        },
        lifecycle_status="draft",
    )
    session.add(module)
    await session.flush()
    await session.commit()
    return module


async def _seed_run_with_card_step(session: AsyncSession, module_id) -> IngestionRunStep:
    sd = SourceDocument(
        title="t",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    run_state = RunStateService(session)
    run = await run_state.start_run(source_document_id=sd.id)
    step = await run_state.start_step(
        run_id=run.id,
        stage=STAGE_CARD_SEARCH_METADATA_GENERATION,
        input_summary={"module_id": str(module_id)},
    )
    await session.commit()
    return step


def _sample_metadata(*, keyword: str = "cough") -> dict:
    return {
        "schema_version": 1,
        "retrieval_hints": {"en": ["child cough"], "bn": []},
        "keywords": {"en": [keyword], "bn": []},
        "synonyms": {"en": {}},
        "questions": {"en": ["When to refer?"], "bn": []},
    }


class TestCardSearchMetadataWorker:
    async def test_persists_metadata_for_all_cards(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        batch_result = CardSearchMetadataBatchResult(
            metadata_by_index={
                0: _sample_metadata(keyword="cough"),
                1: _sample_metadata(keyword="fever"),
            },
            failed_indices=[],
        )

        with patch(
            "platform_service.services.card_search_metadata_generator.CardSearchMetadataGenerator.generate_for_module",
            AsyncMock(return_value=batch_result),
        ):
            count = await generate_card_search_metadata_batch(module.id)

        assert count == 2
        await db_session.refresh(module)
        assert module.module_json["cards"][0]["search_metadata"]["keywords_en"] == ["cough"]
        assert module.module_json["cards"][1]["search_metadata"]["keywords_en"] == ["fever"]

    async def test_skips_cards_with_existing_metadata(self, db_session: AsyncSession) -> None:
        module = await _seed_module(
            db_session,
            cards=[
                {
                    "title": {"bn": "T1"},
                    "body": {"bn": "body1"},
                    "search_metadata": {"keywords": {"en": ["x"]}},
                },
                {"title": {"bn": "T2"}, "body": {"bn": "body2"}},
            ],
        )

        with patch(
            "platform_service.services.card_search_metadata_generator.CardSearchMetadataGenerator.generate_for_module",
            AsyncMock(
                return_value=CardSearchMetadataBatchResult(
                    metadata_by_index={1: _sample_metadata()},
                    failed_indices=[],
                )
            ),
        ) as mock_generate:
            count = await generate_card_search_metadata_batch(module.id)

        assert count == 1
        mock_generate.assert_called_once()
        assert mock_generate.call_args[0][1] == [1]
        await db_session.refresh(module)
        assert module.module_json["cards"][0]["search_metadata"]["keywords_en"] == ["x"]
        assert module.module_json["cards"][1]["search_metadata"]["keywords_en"] == ["cough"]

    async def test_batch_chains_module_search_metadata(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        step = await _seed_run_with_card_step(db_session, module.id)
        metadata_step_id = uuid4()
        embedding_step_id = uuid4()
        mock_module_metadata = MagicMock()

        batch_result = CardSearchMetadataBatchResult(
            metadata_by_index={
                0: _sample_metadata(keyword="cough"),
                1: _sample_metadata(keyword="fever"),
            },
            failed_indices=[],
        )

        with (
            patch(
                "platform_service.services.card_search_metadata_generator.CardSearchMetadataGenerator.generate_for_module",
                AsyncMock(return_value=batch_result),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_search_metadata_task",
                mock_module_metadata,
            ),
        ):
            count = await generate_card_search_metadata_batch(
                module.id,
                card_step_id=step.id,
                metadata_step_id=metadata_step_id,
                embedding_step_id=embedding_step_id,
            )

        assert count == 2
        mock_module_metadata.delay.assert_called_once()
        refreshed_step = await db_session.get(IngestionRunStep, step.id)
        assert refreshed_step is not None
        assert refreshed_step.status == "succeeded"

    async def test_partial_failure_persists_successful_cards(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        step = await _seed_run_with_card_step(db_session, module.id)
        mock_module_metadata = MagicMock()

        batch_result = CardSearchMetadataBatchResult(
            metadata_by_index={0: _sample_metadata(keyword="cough")},
            failed_indices=[1],
        )

        with (
            patch(
                "platform_service.services.card_search_metadata_generator.CardSearchMetadataGenerator.generate_for_module",
                AsyncMock(return_value=batch_result),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_search_metadata_task",
                mock_module_metadata,
            ),
        ):
            count = await generate_card_search_metadata_batch(module.id, card_step_id=step.id)

        assert count == 2
        await db_session.refresh(module)
        assert module.module_json["cards"][0]["search_metadata"]["keywords_en"] == ["cough"]
        assert "search_metadata" not in module.module_json["cards"][1]
        mock_module_metadata.delay.assert_called_once()
        refreshed_step = await db_session.get(IngestionRunStep, step.id)
        assert refreshed_step is not None
        assert refreshed_step.status == "succeeded"
        output = refreshed_step.output_summary_jsonb or {}
        assert output["cards_succeeded_indices"] == [0]
        assert output["cards_failed_indices"] == [1]

    async def test_batch_with_no_cards_chains_module_metadata(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session, cards=[])
        step = await _seed_run_with_card_step(db_session, module.id)
        mock_module_metadata = MagicMock()

        with patch(
            "platform_service.celery_tasks.generate_module_search_metadata_task",
            mock_module_metadata,
        ):
            count = await generate_card_search_metadata_batch(
                module.id,
                card_step_id=step.id,
                metadata_step_id=uuid4(),
                embedding_step_id=uuid4(),
            )

        assert count == 0
        mock_module_metadata.delay.assert_called_once()

    async def test_chain_downstream_false_skips_module_metadata(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        mock_module_metadata = MagicMock()
        batch_result = CardSearchMetadataBatchResult(
            metadata_by_index={
                0: _sample_metadata(keyword="cough"),
                1: _sample_metadata(keyword="fever"),
            },
            failed_indices=[],
        )

        with (
            patch(
                "platform_service.services.card_search_metadata_generator.CardSearchMetadataGenerator.generate_for_module",
                AsyncMock(return_value=batch_result),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_search_metadata_task",
                mock_module_metadata,
            ),
        ):
            count = await generate_card_search_metadata_batch(module.id, chain_downstream=False)

        assert count == 2
        await db_session.refresh(module)
        assert module.module_json["cards"][0]["search_metadata"]["keywords_en"] == ["cough"]
        mock_module_metadata.delay.assert_not_called()
