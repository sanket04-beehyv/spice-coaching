"""Tests for post-publish search metadata worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.module_search_metadata_generator import SearchMetadataResult
from platform_service.workers.search_metadata_worker import generate_search_metadata_for_module
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(text("TRUNCATE module, module_family RESTART IDENTITY CASCADE"))
    await db_session.commit()


async def _seed_module(session: AsyncSession) -> Module:
    fam = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "Test module"},
        domain="rmnch",
        module_type="refresher",
        module_json={"cards": [{"title": {"bn": "T"}, "body": {"bn": "body"}}]},
        lifecycle_status="draft",
    )
    session.add(module)
    await session.flush()
    await session.commit()
    return module


class TestSearchMetadataWorker:
    async def test_persists_metadata_and_enqueues_embedding(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        metadata = {
            "schema_version": 1,
            "keywords": {"en": ["cough"], "bn": []},
            "search_phrases": {"en": ["child cough"], "bn": []},
            "synonyms": {"en": {}},
            "topic_tags": ["respiratory"],
            "clinical_conditions": [],
            "audience": "chw_field_worker",
            "rationale": "ok",
        }
        mock_embed = MagicMock()

        with (
            patch(
                "platform_service.services.module_search_metadata_generator.ModuleSearchMetadataGenerator.generate",
                AsyncMock(return_value=SearchMetadataResult(metadata=metadata)),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_embedding_task",
                mock_embed,
            ),
        ):
            ok = await generate_search_metadata_for_module(
                module.id,
                embedding_step_id=uuid4(),
            )

        assert ok is True
        await db_session.refresh(module)
        assert module.search_metadata_jsonb is not None
        assert module.search_metadata_jsonb["keywords_en"] == ["cough"]
        mock_embed.delay.assert_called_once()

    async def test_chains_embedding_on_generation_failure(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        mock_embed = MagicMock()

        with (
            patch(
                "platform_service.services.module_search_metadata_generator.ModuleSearchMetadataGenerator.generate",
                AsyncMock(return_value=SearchMetadataResult(metadata=None, error="invalid_json")),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_embedding_task",
                mock_embed,
            ),
        ):
            ok = await generate_search_metadata_for_module(module.id, embedding_step_id=uuid4())

        assert ok is False
        await db_session.refresh(module)
        assert module.search_metadata_jsonb is None
        mock_embed.delay.assert_called_once()

    async def test_chains_embedding_when_module_missing(self) -> None:
        mock_embed = MagicMock()
        missing_id = uuid4()

        with patch(
            "platform_service.celery_tasks.generate_module_embedding_task",
            mock_embed,
        ):
            ok = await generate_search_metadata_for_module(missing_id, embedding_step_id=uuid4())

        assert ok is False
        mock_embed.delay.assert_called_once()

    async def test_chain_downstream_false_skips_embedding(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        metadata = {
            "schema_version": 1,
            "keywords": {"en": ["cough"], "bn": []},
            "search_phrases": {"en": ["child cough"], "bn": []},
            "synonyms": {"en": {}},
            "topic_tags": ["respiratory"],
            "clinical_conditions": [],
            "audience": "chw_field_worker",
            "rationale": "ok",
        }
        mock_embed = MagicMock()

        with (
            patch(
                "platform_service.services.module_search_metadata_generator.ModuleSearchMetadataGenerator.generate",
                AsyncMock(return_value=SearchMetadataResult(metadata=metadata)),
            ),
            patch(
                "platform_service.celery_tasks.generate_module_embedding_task",
                mock_embed,
            ),
        ):
            ok = await generate_search_metadata_for_module(
                module.id,
                embedding_step_id=uuid4(),
                chain_downstream=False,
            )

        assert ok is True
        await db_session.refresh(module)
        assert module.search_metadata_jsonb is not None
        mock_embed.delay.assert_not_called()
