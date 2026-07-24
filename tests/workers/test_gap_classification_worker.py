"""Tests for post-publish behavioural gap classification worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.services.module_gap_classifier import ModuleGapClassifier
from platform_service.workers.gap_classification_worker import classify_module_gaps_for_module
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_behavioural_gap, module, module_family, behavioural_gap RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_gap(
    session: AsyncSession,
    *,
    gap_code: str,
    domain: str = "referral",
) -> BehaviouralGap:
    gap = BehaviouralGap(
        gap_code=gap_code,
        description=f"Description for {gap_code}",
        domain=domain,
        severity_default="moderate",
        detection_rule_jsonb={},
        status="active",
    )
    session.add(gap)
    await session.flush()
    return gap


async def _seed_module_with_primary(session: AsyncSession) -> tuple[Module, BehaviouralGap]:
    primary = await _seed_gap(
        session,
        gap_code=f"module_primary_gap_{uuid4().hex}",
        domain="hypertension",
    )
    fam = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "Hypertension referral"},
        description_localized={"en": "Teaches correct BP referral thresholds."},
        domain="hypertension",
        module_type="refresher",
        primary_gap_id=primary.id,
        module_json={
            "cards": [
                {
                    "title": {"bn": "কার্ড"},
                    "body": {"bn": "বিষয়বস্তু"},
                }
            ]
        },
        lifecycle_status="draft",
    )
    session.add(module)
    await session.flush()
    await ModuleGapRepository(session).add_primary_link(module, behavioural_gap_id=primary.id)
    await session.commit()
    return module, primary


def _inference_response(*, gap_codes: list[str], rationale: str = "test") -> InferenceResponse:
    return InferenceResponse(
        request_id="r-gap",
        generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text="",
        parsed_json={
            "associated_gap_codes": gap_codes,
            "rationale": rationale,
        },
        latency_ms=10,
        token_usage=TokenUsage(input=10, output=10),
    )


class TestModuleGapClassifier:
    async def test_drops_unknown_and_module_primary_codes(self, db_session: AsyncSession) -> None:
        module, primary = await _seed_module_with_primary(db_session)
        seeded = await _seed_gap(db_session, gap_code="referral_iccm_danger_signs")
        await _seed_gap(
            db_session,
            gap_code="missed_hypertension_referral_threshold",
            domain="hypertension",
        )
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_inference_response(
                gap_codes=[
                    "referral_iccm_danger_signs",
                    "missed_hypertension_referral_threshold",
                    "nonexistent_gap",
                    primary.gap_code,
                ],
            )
        )
        classifier = ModuleGapClassifier(db_session, client=client)
        result = await classifier.classify_module(module)

        assert result.associated_gap_codes == ["referral_iccm_danger_signs"]
        assert result.associated_gap_ids == [seeded.id]


class TestClassifyModuleGapsWorker:
    async def test_writes_secondary_links_preserves_primary(self, db_session: AsyncSession) -> None:
        module, primary = await _seed_module_with_primary(db_session)
        seeded = await _seed_gap(db_session, gap_code="referral_cbs")
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(return_value=_inference_response(gap_codes=["referral_cbs"]))

        with patch(
            "platform_service.services.module_gap_classifier.AIRuntimeClient",
            return_value=client,
        ):
            count = await classify_module_gaps_for_module(module.id)

        assert count == 1
        await db_session.refresh(module)
        assert module.primary_gap_id == primary.id

        links = (
            (
                await db_session.execute(
                    select(ModuleBehaviouralGap).where(ModuleBehaviouralGap.module_id == module.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(links) == 2
        primary_links = [link for link in links if link.is_primary]
        secondary_links = [link for link in links if not link.is_primary]
        assert len(primary_links) == 1
        assert primary_links[0].behavioural_gap_id == primary.id
        assert {link.behavioural_gap_id for link in secondary_links} == {seeded.id}
        assert module.quality_flags_jsonb is not None
        assert module.quality_flags_jsonb["gap_classification"]["associated_gap_codes"] == ["referral_cbs"]

    async def test_empty_registry_succeeds_with_zero_secondaries(self, db_session: AsyncSession) -> None:
        module, primary = await _seed_module_with_primary(db_session)
        await db_session.execute(
            text(
                "DELETE FROM behavioural_gap "
                "WHERE domain = 'referral' AND gap_code NOT LIKE 'module_primary_gap_%'"
            )
        )
        await db_session.commit()

        count = await classify_module_gaps_for_module(module.id)
        assert count == 0

        links = (
            (
                await db_session.execute(
                    select(ModuleBehaviouralGap).where(ModuleBehaviouralGap.module_id == module.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(links) == 1
        assert links[0].behavioural_gap_id == primary.id
        assert links[0].is_primary is True
