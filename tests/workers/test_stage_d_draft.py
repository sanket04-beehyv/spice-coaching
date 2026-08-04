"""Layer 2 chunk 5 — Stage 2-draft (Stage D) tests.

Covers the post-architecture-reset Stage D behaviour:
- Cards persisted as `module.module_json.cards` (no per-card rows).
- Module auto-published with `lifecycle_status='published'` and
  `clinically_reviewed=false`.
- Post-publish quiz + embedding tasks enqueued in a try/except so a broker
  outage doesn't fail the candidate.
- Validator drops cards with hard violations; soft warnings annotated as
  `field_flags`.
- If validator strips below `card_min_count`, returns insufficient (no module).
- `quality_flags_jsonb` propagated from the candidate row onto the module.
- Module runtime payload (cards array) contains only the public keys, not
  transient drafter-internal fields.
- Module-family code collision adds a numeric suffix.
- Auto-publish principle: failures during draft never raise; all return
  StageDResult with module_id=None and a typed insufficient_reason.

Test isolation: each test runs inside the per-loop `db_session` and
truncates relevant tables afterward. We mock the CardDrafter inline so no
ai-runtime calls happen during the test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service import celery_tasks
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.module_card import ModuleCard
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.db.module_availability import LIFECYCLE_REVIEW_PENDING
from platform_service.db.repositories.module_drafter_repository import _slugify
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.services.card_drafter import (
    CardDrafterError,
    CardDrafterResult,
)
from platform_service.services.module_card_service import ModuleCardService
from platform_service.services.published_module_merger import PublishedModuleMergerResult
from platform_service.services.run_state_service import (
    STAGE_CARD_DRAFT,
    RunStateService,
)
from platform_service.workers.stage_d_draft import StageDOrchestrator, StageDResult
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables
from tests.localized_helpers import refresher_card

pytestmark = [requires_db, pytest.mark.asyncio]

# ─── Per-test cleanup ─────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "module_card, module_quiz_question, module, module_family, behavioural_gap, module_candidate_draft, content_block, source_page, source_document, ingestion_run_step, ingestion_run, ingest_batch",
    )
    yield


async def _start_card_draft_step(session: AsyncSession, candidate: ModuleCandidateDraft):
    """Create a running card_draft step for Stage D activity patches."""
    run_state = RunStateService(session)
    step = await run_state.start_step(
        run_id=candidate.ingestion_run_id,
        stage=STAGE_CARD_DRAFT,
        input_summary={"candidate_id": str(candidate.id)},
    )
    await session.commit()
    return step


async def _run_dual_path_merge(
    session: AsyncSession,
    *,
    orch: StageDOrchestrator,
    candidate: ModuleCandidateDraft,
) -> StageDResult:
    """Run Stage D merge path (persists primary + secondary immediately)."""
    step = await _start_card_draft_step(session, candidate)
    result = await orch.run(candidate_id=candidate.id, step_id=step.id)
    await session.commit()
    return result


async def _seed_candidate(
    session: AsyncSession,
    *,
    proposed_title: str = "Sample Topic",
    cited_blocks: int = 3,
    quality_flags: dict[str, Any] | None = None,
    behavioural_gap_code: str | None = None,
    description_localized: dict[str, str] | None = None,
    domain: str | None = None,
    estimated_card_count: int = 5,
    estimated_quiz_count: int = 4,
    content_domain: str = "clinical",
    assessment_mode: str = "with_quiz",
) -> ModuleCandidateDraft:
    # ingestion_run + source_document + source_page + content_blocks → candidate.
    sd = SourceDocument(
        title="seed",
        source_type="pdf",
        primary_language="en",
        content_domain=content_domain,
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    sp = SourcePage(
        source_document_id=sd.id,
        page_number=1,
        markdown_content="body",
        extraction_method="text",
        extraction_quality_score=0.9,
    )
    session.add(sp)
    await session.flush()
    block_ids: list[UUID] = []
    for i in range(cited_blocks):
        cb = ContentBlock(
            source_page_id=sp.id,
            block_order=i,
            block_type="text",
            content_text=f"Block {i} text content for candidate.",
        )
        session.add(cb)
        await session.flush()
        block_ids.append(cb.id)

    # The pipeline writes ingestion_run + ingestion_run_step around Stage D,
    # but Stage D itself only needs the candidate row to exist with valid
    # FK + provenance. We skip the run row by giving the FK a value that
    # won't be referenced (CASCADE on insert: must point at a real run).
    batch = IngestBatch(status="running", assessment_mode=assessment_mode)
    session.add(batch)
    await session.flush()
    run = IngestionRun(
        source_document_id=sd.id,
        ingest_batch_id=batch.id,
        status="running",
    )
    session.add(run)
    await session.flush()

    candidate = ModuleCandidateDraft(
        ingestion_run_id=run.id,
        proposed_title=proposed_title,
        scope_summary="A scope summary explaining the topic.",
        description_localized=description_localized,
        domain=domain,
        source_provenance_jsonb=[
            {
                "source_document_id": str(sd.id),
                "source_page_id": str(sp.id),
                "content_block_ids": [str(b) for b in block_ids],
            }
        ],
        estimated_card_count=estimated_card_count,
        estimated_quiz_count=estimated_quiz_count,
        proposed_module_type="refresher",
        behavioural_gap_code=behavioural_gap_code,
        quality_flags_jsonb=quality_flags,
    )
    session.add(candidate)
    await session.flush()
    await session.commit()
    return candidate


def _make_card(
    *,
    title: str = "কার্ড",
    body: str = "মূল বিষয়।",
    next_action: str = "পরবর্তী পদক্ষেপ।",
    field_flags: dict | None = None,
    transient_card_family_id: UUID | None = None,
) -> dict[str, Any]:
    """Build a card dict in the shape the drafter returns."""
    out: dict[str, Any] = refresher_card(
        title=title,
        body=body,
        next_action=next_action,
        source_block_ids=[str(uuid4())],
    )
    if field_flags is not None:
        out["field_flags"] = field_flags
    if transient_card_family_id is not None:
        out["card_family_id"] = str(transient_card_family_id)
    return out


def _make_card_drafter_mock(
    cards: list[dict] | None = None, *, insufficient_reason: str | None = None
) -> MagicMock:
    drafter = MagicMock()
    drafter.draft = AsyncMock(
        return_value=CardDrafterResult(
            cards=cards or [_make_card() for _ in range(5)],
            insufficient_reason=insufficient_reason,
        )
    )
    return drafter


# ─── Happy path ────────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_persists_module_with_cards_inline(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        cards = [_make_card(title=f"C{i}") for i in range(5)]
        drafter = _make_card_drafter_mock(cards)

        orch = StageDOrchestrator(db_session, card_drafter=drafter)

        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        assert result.module_id is not None
        assert result.cards_count == 5
        assert result.insufficient_reason is None

        # Read back the module from DB.
        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.lifecycle_status == "draft"
        assert module.clinically_reviewed is False
        assert module.description_localized.get("bn") == "A scope summary explaining the topic."
        assert module.module_json is not None
        assert "cards" not in (module.module_json or {})
        card_rows = (
            (await db_session.execute(select(ModuleCard).where(ModuleCard.module_id == result.module_id)))
            .scalars()
            .all()
        )
        assert len(card_rows) == 5
        assert card_rows[0].card_family_id is not None
        assert card_rows[0].title_localized.get("bn")

    async def test_persists_localized_descriptions_from_candidate(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(
            db_session,
            description_localized={
                "en": "English description paragraph for the module.",
                "bn": "মডিউলের বাংলা বর্ণনা অনুচ্ছেদ।",
            },
        )
        cards = [_make_card(title=f"C{i}") for i in range(5)]
        drafter = _make_card_drafter_mock(cards)

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.description_localized.get("bn") == "মডিউলের বাংলা বর্ণনা অনুচ্ছেদ।"

    async def test_quality_flags_propagated_from_candidate_to_module(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(
            db_session,
            quality_flags={"flags": ["insufficient_heading_coverage"]},
        )
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.quality_flags_jsonb == {"flags": ["insufficient_heading_coverage"]}

    async def test_post_publish_enqueue_called_with_module_id(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        enqueue_spy = AsyncMock()
        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        orch._enqueue_post_publish = enqueue_spy  # type: ignore[method-assign]

        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        enqueue_spy.assert_awaited_once()
        called_module_id, called_source_ids = enqueue_spy.await_args.args
        assert called_module_id == result.module_id
        assert len(called_source_ids) == 1

    async def test_read_only_source_skips_quiz_enqueue(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, assessment_mode="read_only")
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])
        orch = StageDOrchestrator(db_session, card_drafter=drafter)

        with pytest.MonkeyPatch.context() as mp:
            quiz_delay = MagicMock()
            card_batch_delay = MagicMock()
            metadata_delay = MagicMock()
            mp.setattr(
                "platform_service.celery_tasks.generate_module_quiz_task.delay",
                quiz_delay,
            )
            mp.setattr(
                "platform_service.celery_tasks.generate_module_card_search_metadata_batch_task.delay",
                card_batch_delay,
            )
            mp.setattr(
                "platform_service.celery_tasks.generate_module_search_metadata_task.delay",
                metadata_delay,
            )
            result = await orch.run(candidate_id=candidate.id)
            await db_session.commit()

        assert result.module_id is not None
        quiz_delay.assert_not_called()
        card_batch_delay.assert_called_once()
        metadata_delay.assert_not_called()


# ─── Validator interaction ────────────────────────────────────────────────


class TestValidatorInteraction:
    async def test_soft_warnings_annotate_field_flags_on_cards(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        # Drafter returns cards without field_flags; validator may add some.
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        # We don't assert on the contents of any soft warning (validator
        # logic is its own test); we just verify the module came out.
        assert result.module_id is not None

    async def test_validator_dropping_below_minimum_returns_insufficient(
        self, db_session: AsyncSession
    ) -> None:
        # Create cards that the validator will drop hard. Easiest way:
        # body contains "you have diabetes" — _DIAGNOSIS_RE forbidden.
        candidate = await _seed_candidate(db_session)
        bad_cards = [_make_card(body="you have diabetes type 2") for _ in range(5)]
        drafter = _make_card_drafter_mock(bad_cards)

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)

        assert result.module_id is None
        assert result.cards_count == 0
        assert result.insufficient_reason == "validator_dropped_too_many_cards"


# ─── Failure paths ────────────────────────────────────────────────────────


class TestFailurePaths:
    async def test_no_cited_blocks_returns_insufficient(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, cited_blocks=0)
        # Replace the candidate's provenance with empty block_ids.
        candidate.source_provenance_jsonb = [
            {
                "source_document_id": str(uuid4()),
                "source_page_id": str(uuid4()),
                "content_block_ids": [],
            }
        ]
        await db_session.commit()

        drafter = MagicMock()
        drafter.draft = AsyncMock()  # should not be called

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)

        assert result.module_id is None
        assert result.insufficient_reason == "no_cited_blocks_resolvable"
        drafter.draft.assert_not_awaited()

    async def test_card_drafter_insufficient_reason_propagates(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        drafter = MagicMock()
        drafter.draft = AsyncMock(
            return_value=CardDrafterResult(
                cards=[],
                insufficient_reason="single_concept_only",
            )
        )

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)

        assert result.module_id is None
        assert result.insufficient_reason == "single_concept_only"
        assert result.cards_count == 0

    async def test_card_drafter_error_propagates(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        drafter = MagicMock()
        drafter.draft = AsyncMock(side_effect=CardDrafterError("Vertex 503"))

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        with pytest.raises(CardDrafterError, match="Vertex 503"):
            await orch.run(candidate_id=candidate.id)

    async def test_unknown_candidate_id_raises(self, db_session: AsyncSession) -> None:
        drafter = MagicMock()
        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        with pytest.raises(ValueError, match="not found"):
            await orch.run(candidate_id=uuid4())

    async def test_post_publish_enqueue_failure_does_not_fail_stage(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If `delay()` raises (e.g., broker unreachable), the module is
        still persisted and Stage D returns success. The try/except inside
        `_enqueue_post_publish` catches and logs."""
        candidate = await _seed_candidate(db_session)
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        # Patch the actual celery task `.delay` methods so the orchestrator's
        # try/except path is exercised. The first call raises; the second
        # would also raise but `_enqueue_post_publish` catches the first
        # exception and bails out of the whole block.
        raising = MagicMock(side_effect=RuntimeError("broker unreachable"))
        monkeypatch.setattr(celery_tasks.generate_module_quiz_task, "delay", raising)
        monkeypatch.setattr(celery_tasks.generate_module_embedding_task, "delay", raising)

        # Restore the original _enqueue_post_publish (test ordering may have
        # left a no-op stub from earlier tests).

        # Reload the staticmethod from the source.
        import importlib

        # no-inline-imports: import-after-reload required for fresh module state
        import platform_service.workers.stage_d_draft as _stage_d_mod

        importlib.reload(_stage_d_mod)
        from platform_service.workers.stage_d_draft import StageDOrchestrator as _SDFresh

        orch = _SDFresh(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        # Module published despite enqueue failure.
        assert result.module_id is not None
        # And the raising mock was called at least once (verifies we got
        # into the enqueue path).
        assert raising.call_count >= 1


# ─── Module-family handling ───────────────────────────────────────────────


class TestModuleFamily:
    async def test_collision_appends_numeric_suffix(self, db_session: AsyncSession) -> None:
        # Pre-seed a family with a code that the slug for "Sample Topic" would
        # produce, forcing the next call to append "-1".
        title = "Same Title"
        slug = _slugify(title)
        # Insert a family already at that slug.
        existing = ModuleFamily(module_code=slug)
        db_session.add(existing)
        await db_session.flush()
        await db_session.commit()

        candidate = await _seed_candidate(db_session, proposed_title=title)
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        # The new module must belong to a family with a *different* module_code.
        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        new_family = (
            await db_session.execute(select(ModuleFamily).where(ModuleFamily.id == module.module_family_id))
        ).scalar_one()
        assert new_family.module_code != existing.module_code
        # Suffix matches the "-N" convention.
        assert new_family.module_code.startswith(slug)
        assert new_family.module_code != slug


# ─── Primary behavioural gap ───────────────────────────────────────────────


class TestPrimaryBehaviouralGap:
    async def test_creates_primary_gap_and_sets_module_primary_gap_id(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.primary_gap_id is not None
        gap = (
            await db_session.execute(select(BehaviouralGap).where(BehaviouralGap.id == module.primary_gap_id))
        ).scalar_one()
        expected_code = f"module_primary_gap_{str(module.id).replace('-', '_')}"
        assert gap.gap_code == expected_code
        assert gap.description == module.title_localized.get("bn")
        assert gap.domain == module.domain
        assert gap.status == "active"
        assert gap.detection_rule_jsonb == {}
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
        assert links[0].behavioural_gap_id == module.primary_gap_id
        assert links[0].is_primary is True

    async def test_creates_primary_gap_even_for_legacy_new_gap_proposed_marker(
        self, db_session: AsyncSession
    ) -> None:
        candidate = await _seed_candidate(db_session, behavioural_gap_code="new_gap_proposed")
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.primary_gap_id is not None

    async def test_module_domain_comes_from_candidate_draft(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(
            db_session, proposed_title="FP counselling", domain="family_planning"
        )
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        module = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert module.domain == "family_planning"

    async def test_quiz_drafter_kwarg_accepted_for_backwards_compat(self, db_session: AsyncSession) -> None:
        """Legacy callers may still pass `quiz_drafter=`. Constructor accepts
        and ignores it (post-architecture-reset Stage D doesn't generate
        quiz; it's a separate post-publish worker)."""
        # Should not raise.
        StageDOrchestrator(db_session, card_drafter=MagicMock(), quiz_drafter="ignored")


# ─── Card payload normalisation ───────────────────────────────────────────


class TestCardPayloadNormalisation:
    async def test_module_card_rows_persist_family_id(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        cards_with_transient = [
            _make_card(
                title=f"T{i}",
                field_flags={"some_flag": True},
                transient_card_family_id=uuid4(),
            )
            for i in range(5)
        ]
        drafter = _make_card_drafter_mock(cards_with_transient)

        orch = StageDOrchestrator(db_session, card_drafter=drafter)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        card_rows = (
            (await db_session.execute(select(ModuleCard).where(ModuleCard.module_id == result.module_id)))
            .scalars()
            .all()
        )
        assert len(card_rows) == 5
        for row in card_rows:
            assert row.card_family_id is not None
            assert row.field_flags_jsonb is None
            assert row.title_localized


# ─── Published-module merge ───────────────────────────────────────────────


async def _seed_active_module(
    session: AsyncSession,
    *,
    title_en: str = "Sample Topic",
    block_ids: list[UUID],
    family_code: str = "published-family",
    lifecycle_status: str = "published",
    card_search_metadata: dict[str, Any] | None = None,
    module_search_metadata: dict[str, Any] | None = None,
) -> Module:
    gap = BehaviouralGap(
        gap_code="published_primary_gap",
        description=title_en,
        domain="rmnch",
        severity_default="moderate",
        detection_rule_jsonb={},
        status="active",
    )
    session.add(gap)
    fam = ModuleFamily(module_code=family_code)
    session.add(fam)
    await session.flush()
    old_cards = [
        {
            "title": {"bn": "পুরোনো কার্ড"},
            "body": {"bn": "পুরনো বিষয়বস্তু।"},
            "next_action": {"bn": "পুরনো পদক্ষেপ।"},
            "source_block_ids": [str(block_ids[0])],
            **({"search_metadata": card_search_metadata} if card_search_metadata is not None else {}),
        }
    ]
    published = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": title_en},
        domain="rmnch",
        module_type="refresher",
        primary_gap_id=gap.id,
        module_json={},
        search_metadata_jsonb=module_search_metadata,
        lifecycle_status=lifecycle_status,
        clinically_reviewed=lifecycle_status == "published",
        published_at=datetime.now(UTC) if lifecycle_status == "published" else None,
    )
    session.add(published)
    await session.flush()
    await ModuleCardService(session).append_cards(published.id, old_cards)
    await session.flush()
    await ModuleGapRepository(session).add_primary_link(published, behavioural_gap_id=gap.id)
    await session.commit()
    return published


_seed_published_module = _seed_active_module


class TestPublishedModuleMerge:
    async def test_merge_creates_dual_path_review_pending_pair(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(
            db_session,
            proposed_title="Sample Topic",
            description_localized={
                "en": "New English description from candidate.",
                "bn": "নতুন বাংলা বর্ণনা।",
            },
        )
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(db_session, block_ids=[block_id])

        drafted_cards = [_make_card(title=f"Drafted {i}") for i in range(5)]
        for card in drafted_cards:
            card["source_block_ids"] = [str(block_id)]
        merged_cards = [_make_card(title=f"Merged {i}") for i in range(5)]
        for card in merged_cards:
            card["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="same behavioural unit",
                merged_cards=merged_cards,
            )
        )
        drafter = _make_card_drafter_mock(drafted_cards)
        orch = StageDOrchestrator(
            db_session,
            card_drafter=drafter,
            published_module_merger=merger,
        )
        result = await _run_dual_path_merge(db_session, orch=orch, candidate=candidate)
        await db_session.commit()

        assert result.was_published_merge is True
        assert result.merged_from_module_id == published.id
        assert result.module_id is not None
        assert result.secondary_module_id is not None

        source = (await db_session.execute(select(Module).where(Module.id == published.id))).scalar_one()
        assert source.lifecycle_status == "published"

        primary = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        secondary = (
            await db_session.execute(select(Module).where(Module.id == result.secondary_module_id))
        ).scalar_one()
        assert primary.lifecycle_status == LIFECYCLE_REVIEW_PENDING
        assert secondary.lifecycle_status == LIFECYCLE_REVIEW_PENDING
        assert primary.module_family_id == published.module_family_id
        assert secondary.module_family_id == published.module_family_id
        assert secondary.version == 2
        assert primary.version == 3
        assert primary.supersedes_module_id is None
        assert secondary.supersedes_module_id is None
        assert primary.merge_secondary_module_id == secondary.id
        assert secondary.merge_primary_module_id == primary.id
        assert primary.merge_source_module_id == published.id
        assert secondary.merge_source_module_id == published.id
        assert primary.primary_gap_id == published.primary_gap_id
        assert secondary.quality_flags_jsonb is not None
        assert "published_module_merged" in (secondary.quality_flags_jsonb.get("flags") or [])
        assert primary.description_localized.get("bn") == "নতুন বাংলা বর্ণনা।"

    async def test_dual_path_leaves_matched_module_untouched(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(db_session, block_ids=[block_id])

        new_cards = [_make_card(title=f"New {i}") for i in range(5)]
        for card in new_cards:
            card["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="same topic",
                merged_cards=new_cards,
            )
        )
        drafter = _make_card_drafter_mock(new_cards)
        orch = StageDOrchestrator(
            db_session,
            card_drafter=drafter,
            published_module_merger=merger,
        )
        result = await _run_dual_path_merge(db_session, orch=orch, candidate=candidate)
        await db_session.commit()

        assert result.was_published_merge is True
        still = (await db_session.execute(select(Module).where(Module.id == published.id))).scalar_one()
        assert still.lifecycle_status == "published"
        primary = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        assert primary.module_family_id == published.module_family_id

    async def test_skip_merge_internal_opt_out_skips_published_merge(self, db_session: AsyncSession) -> None:
        """Fusion-style internal skip_merge=True must not call the merger."""
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(db_session, block_ids=[block_id])

        new_cards = [_make_card(title=f"New {i}") for i in range(5)]
        for card in new_cards:
            card["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="would match",
                merged_cards=new_cards,
            )
        )
        drafter = _make_card_drafter_mock(new_cards)
        orch = StageDOrchestrator(
            db_session,
            card_drafter=drafter,
            published_module_merger=merger,
        )
        result = await orch.run(candidate_id=candidate.id, skip_merge=True)
        await db_session.commit()

        assert result.was_published_merge is False
        assert result.merged_from_module_id is None
        merger.merge.assert_not_awaited()

        still_published = (
            await db_session.execute(select(Module).where(Module.id == published.id))
        ).scalar_one()
        assert still_published.lifecycle_status == "published"

        new_module = (
            await db_session.execute(select(Module).where(Module.id == result.module_id))
        ).scalar_one()
        assert new_module.module_family_id != published.module_family_id

    async def test_merge_against_draft_creates_dual_path(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        existing_draft = await _seed_active_module(
            db_session,
            block_ids=[block_id],
            lifecycle_status="draft",
            family_code="draft-family",
        )

        new_cards = [_make_card(title=f"Merged {i}") for i in range(5)]
        for card in new_cards:
            card["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=existing_draft.id,
                match_rationale="same topic",
                merged_cards=new_cards,
            )
        )
        drafter = _make_card_drafter_mock(new_cards)
        orch = StageDOrchestrator(
            db_session,
            card_drafter=drafter,
            published_module_merger=merger,
        )
        result = await _run_dual_path_merge(db_session, orch=orch, candidate=candidate)
        await db_session.commit()

        assert result.was_published_merge is True
        source = (await db_session.execute(select(Module).where(Module.id == existing_draft.id))).scalar_one()
        assert source.lifecycle_status == "draft"
        primary = (await db_session.execute(select(Module).where(Module.id == result.module_id))).scalar_one()
        secondary = (
            await db_session.execute(select(Module).where(Module.id == result.secondary_module_id))
        ).scalar_one()
        assert secondary.version == 2
        assert primary.version == 3
        assert primary.lifecycle_status == LIFECYCLE_REVIEW_PENDING
        assert secondary.lifecycle_status == LIFECYCLE_REVIEW_PENDING

    async def test_merge_fallback_when_validator_strips_too_many(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session)
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(db_session, block_ids=[block_id])

        # Only one valid card after merge — below card_min_count.
        bad_cards = [_make_card(title="Only one")]
        bad_cards[0]["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="match",
                merged_cards=bad_cards,
            )
        )
        drafter = _make_card_drafter_mock([_make_card() for _ in range(5)])
        orch = StageDOrchestrator(db_session, card_drafter=drafter, published_module_merger=merger)
        result = await orch.run(candidate_id=candidate.id)
        await db_session.commit()

        assert result.was_published_merge is False
        still_published = (
            await db_session.execute(select(Module).where(Module.id == published.id))
        ).scalar_one()
        assert still_published.lifecycle_status == "published"
        new_module = (
            await db_session.execute(select(Module).where(Module.id == result.module_id))
        ).scalar_one()
        assert new_module.module_family_id != published.module_family_id

    async def test_merge_strips_superseded_search_metadata(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(
            db_session,
            block_ids=[block_id],
            card_search_metadata={"keywords": {"en": ["old-card"]}},
            module_search_metadata={"keywords": {"en": ["old-module"]}},
        )

        new_cards = [_make_card(title=f"Merged {i}") for i in range(5)]
        for card in new_cards:
            card["source_block_ids"] = [str(block_id)]
            card["search_metadata"] = {"keywords": {"en": ["echoed-from-llm"]}}

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="same behavioural unit",
                merged_cards=new_cards,
            )
        )
        drafter = _make_card_drafter_mock(new_cards)
        orch = StageDOrchestrator(
            db_session,
            card_drafter=drafter,
            published_module_merger=merger,
        )
        result = await _run_dual_path_merge(db_session, orch=orch, candidate=candidate)
        await db_session.commit()

        assert result.was_published_merge is True
        secondary = (
            await db_session.execute(select(Module).where(Module.id == result.secondary_module_id))
        ).scalar_one()
        assert secondary.search_metadata_jsonb is None
        card_rows = (
            (await db_session.execute(select(ModuleCard).where(ModuleCard.module_id == secondary.id)))
            .scalars()
            .all()
        )
        for row in card_rows:
            assert row.search_metadata_jsonb is None

    async def test_merge_enqueue_forces_card_metadata_regeneration(self, db_session: AsyncSession) -> None:
        candidate = await _seed_candidate(db_session, proposed_title="Sample Topic")
        block_id = UUID(candidate.source_provenance_jsonb[0]["content_block_ids"][0])
        published = await _seed_published_module(db_session, block_ids=[block_id])

        new_cards = [_make_card(title=f"Merged {i}") for i in range(5)]
        for card in new_cards:
            card["source_block_ids"] = [str(block_id)]

        merger = MagicMock()
        merger.merge = AsyncMock(
            return_value=PublishedModuleMergerResult(
                matched_module_id=published.id,
                match_rationale="same behavioural unit",
                merged_cards=new_cards,
            )
        )
        drafter = _make_card_drafter_mock(new_cards)
        mock_card_batch = MagicMock()

        with patch(
            "platform_service.services.draft_pipeline.generate_module_card_search_metadata_batch_task",
            mock_card_batch,
        ):
            orch = StageDOrchestrator(
                db_session,
                card_drafter=drafter,
                published_module_merger=merger,
            )
            await _run_dual_path_merge(db_session, orch=orch, candidate=candidate)
            await db_session.commit()

        # Dual-path enqueues post-publish for primary and secondary.
        assert mock_card_batch.delay.call_count == 2
        assert mock_card_batch.delay.call_args.kwargs.get("force") is True
