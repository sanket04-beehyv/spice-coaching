"""Direct tests for ModuleGapClassifier and payload helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.module_gap_classifier import (
    GapClassificationResult,
    ModuleGapClassifier,
    module_payload_for_classification,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_gaps(db_session: AsyncSession):
    yield
    await db_session.rollback()
    await db_session.execute(text("TRUNCATE module, module_family, behavioural_gap RESTART IDENTITY CASCADE"))
    await db_session.commit()


async def _seed_gap(
    session: AsyncSession,
    *,
    gap_code: str,
    domain: str = "referral",
    status: str = "active",
) -> BehaviouralGap:
    gap = BehaviouralGap(
        gap_code=gap_code,
        description=f"desc {gap_code}",
        domain=domain,
        detection_rule_jsonb={},
        status=status,
    )
    session.add(gap)
    await session.flush()
    return gap


async def _seed_module(session: AsyncSession) -> Module:
    fam = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "Referral module"},
        description_localized={"en": "Teaches referral thresholds."},
        domain="hypertension",
        module_type="refresher",
        module_json={
            "cards": [
                {
                    "title": {"bn": "কার্ড"},
                    "body": {"bn": "x"} * 500,
                    "next_action": {"bn": "act"},
                }
            ]
        },
        lifecycle_status="draft",
    )
    session.add(module)
    await session.flush()
    await session.commit()
    return module


def _inference_response(*, gap_codes: list[str], rationale: str = "ok") -> InferenceResponse:
    return InferenceResponse(
        request_id="r-gap",
        generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
        provider="openai",
        model="gpt-4o-mini",
        raw_text="",
        parsed_json={"associated_gap_codes": gap_codes, "rationale": rationale},
        latency_ms=1,
        token_usage=TokenUsage(input=1, output=1),
    )


class TestModulePayloadForClassification:
    def test_truncates_long_card_fields(self) -> None:
        module = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={"bn": "t"},
            domain="rmnch",
            module_type="refresher",
            module_json={
                "cards": [
                    {
                        "title": {"bn": "T"},
                        "body": {"bn": "b"} * 500 * 500,
                        "next_action": {"bn": "n"} * 300,
                    }
                ]
            },
        )
        payload = module_payload_for_classification(module)
        assert payload["cards"][0]["body"]["bn"].endswith("...")
        assert len(payload["cards"][0]["body"]["bn"]) == 400
        assert len(payload["cards"][0]["next_action"]["bn"]) == 200

    def test_skips_non_dict_cards(self) -> None:
        module = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={"bn": "t"},
            domain="rmnch",
            module_type="refresher",
            module_json={"cards": ["not-a-dict", {"title": {"bn": "ok"}}]},
        )
        payload = module_payload_for_classification(module)
        assert len(payload["cards"]) == 1
        assert payload["cards"][0]["title"]["bn"] == "ok"


@pytest.mark.asyncio
class TestLoadRegistryGaps:
    async def test_filters_primary_prefix_and_non_referral_domain(self, db_session: AsyncSession) -> None:
        await _seed_gap(db_session, gap_code="referral_valid")
        await _seed_gap(db_session, gap_code=f"module_primary_gap_{uuid4().hex[:6]}")
        await _seed_gap(
            db_session,
            gap_code="hypertension_only",
            domain="hypertension",
        )
        await db_session.commit()

        classifier = ModuleGapClassifier(db_session, client=MagicMock())
        registry = await classifier.load_registry_gaps()
        codes = {g.gap_code for g in registry}
        assert codes == {"referral_valid"}


@pytest.mark.asyncio
class TestClassifyModule:
    async def test_empty_registry_returns_rationale(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        await db_session.execute(text("DELETE FROM behavioural_gap WHERE domain = 'referral'"))
        await db_session.commit()

        client = MagicMock()
        classifier = ModuleGapClassifier(db_session, client=client)
        result = await classifier.classify_module(module)

        assert result == GapClassificationResult(
            associated_gap_ids=[],
            associated_gap_codes=[],
            rationale="No active referral-domain behavioural gaps in registry.",
        )
        client.generate.assert_not_called()

    async def test_ai_runtime_error_returns_empty(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        await _seed_gap(db_session, gap_code="referral_one")
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r",
                generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
                provider="openai",
                model="m",
                raw_text="",
                error="provider down",
                latency_ms=1,
                token_usage=TokenUsage(input=0, output=0),
            )
        )
        result = await ModuleGapClassifier(db_session, client=client).classify_module(module)
        assert result.associated_gap_codes == []
        assert result.rationale == ""

    async def test_invalid_json_returns_empty(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        await _seed_gap(db_session, gap_code="referral_one")
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r",
                generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
                provider="openai",
                model="m",
                raw_text="not json",
                parsed_json=None,
                latency_ms=1,
                token_usage=TokenUsage(input=1, output=1),
            )
        )
        result = await ModuleGapClassifier(db_session, client=client).classify_module(module)
        assert result.associated_gap_codes == []

    async def test_drops_unknown_and_primary_codes(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        valid = await _seed_gap(db_session, gap_code="referral_iccm")
        primary = await _seed_gap(
            db_session,
            gap_code=f"module_primary_gap_{uuid4().hex[:6]}",
            domain="referral",
        )
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_inference_response(
                gap_codes=["referral_iccm", "bogus_code", primary.gap_code],
                rationale="matched",
            )
        )
        result = await ModuleGapClassifier(db_session, client=client).classify_module(module)
        assert result.associated_gap_codes == ["referral_iccm"]
        assert result.associated_gap_ids == [valid.id]
        assert result.rationale == "matched"

    async def test_respects_max_associations(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "gap_classification_max_associations", 1)

        module = await _seed_module(db_session)
        await _seed_gap(db_session, gap_code="referral_a")
        await _seed_gap(db_session, gap_code="referral_b")
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(return_value=_inference_response(gap_codes=["referral_a", "referral_b"]))
        result = await ModuleGapClassifier(db_session, client=client).classify_module(module)
        assert len(result.associated_gap_codes) == 1

    async def test_non_list_gap_codes_treated_as_empty(self, db_session: AsyncSession) -> None:
        module = await _seed_module(db_session)
        await _seed_gap(db_session, gap_code="referral_only")
        await db_session.commit()

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r",
                generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
                provider="openai",
                model="m",
                raw_text="",
                parsed_json={"associated_gap_codes": "not-a-list", "rationale": ""},
                latency_ms=1,
                token_usage=TokenUsage(input=1, output=1),
            )
        )
        result = await ModuleGapClassifier(db_session, client=client).classify_module(module)
        assert result.associated_gap_codes == []
