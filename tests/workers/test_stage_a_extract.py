"""Layer 2 chunk 5 — Stage 1 (Stage A) extraction tests.

Focused on the architecture-reset regressions:

- **Empty outline is acceptable** (post-c5b6635): the chunker treats the
  outline as a hint, not an anchor. Stage 1 logs a warning and proceeds.
- **Pages persist** — the smoke-loop commit-before-raise invariant.
- Calibration paths: text_only (clean text), all_vision (force every page
  through the LLM), per_page (mixed). Verifies extraction_method values
  on persisted source_pages.

We mock the text_extractor and vision_extractor so no real PDFs are
needed. The page_renderer is also mocked so vision-path tests don't touch
the filesystem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from platform_service.config import get_settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.workers.extractors.calibration import CalibrationDecision
from platform_service.workers.extractors.media_splitter import MediaChunk
from platform_service.workers.extractors.text_extractor import ExtractedPage
from platform_service.workers.extractors.vision_extractor import VisionExtractionError, VisionExtractionResult
from platform_service.workers.stage_a_extract import (
    Stage1DocumentEmptyError,
    Stage1RecoveryFailedError,
    StageAExtractor,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session, "content_block, source_page, source_document, ingestion_run_step, ingestion_run"
    )
    yield


async def _seed_source_document(session: AsyncSession) -> UUID:
    sd = SourceDocument(
        title="seed",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    await session.commit()
    return sd.id


def _make_stage_a(
    session: AsyncSession,
    *,
    text_pages: list[ExtractedPage],
    vision_markdown: str = "# Heading\n\nBody text",
    page_count: int | None = None,
) -> StageAExtractor:
    """Build a StageAExtractor with mocked text_extractor + vision_extractor +
    page_renderer."""
    if page_count is None:
        page_count = len(text_pages)

    text_extractor_fn = MagicMock(return_value=text_pages)

    page_renderer = MagicMock(return_value=b"FAKE_PNG")

    vision = MagicMock()
    vision.extract_page = AsyncMock(
        return_value=VisionExtractionResult(
            markdown=vision_markdown,
            raw_response=MagicMock(),
        )
    )

    # Patch count_pages at module level too so calibration sees the right total.
    extractor = StageAExtractor(
        session,
        vision_extractor=vision,
        page_renderer=page_renderer,
        text_extractor_fn=text_extractor_fn,
    )
    return extractor


def _good_text_page(page_number: int, *, has_heading: bool = True) -> ExtractedPage:
    body = (
        "# Page Heading\n\n" if has_heading else ""
    ) + "This is well-extracted clean English text content with several lines."
    return ExtractedPage(page_number=page_number, markdown=body)


# ─── Media transcript path ───────────────────────────────────────────────


class TestMediaTranscriptPath:
    async def test_audio_transcript_skips_calibration_and_vision(self, db_session: AsyncSession) -> None:
        sd = SourceDocument(
            title="audio",
            source_type="audio",
            primary_language="bn",
            content_domain="clinical",
            original_storage_path="/tmp/audio.mp3",
        )
        db_session.add(sd)
        await db_session.flush()
        await db_session.commit()

        media_transcriber = AsyncMock(return_value="This English transcript should not be routed to vision.")
        page_renderer = MagicMock(return_value=b"SHOULD_NOT_RENDER")
        vision = MagicMock()
        vision.extract_page = AsyncMock()

        stage_a = StageAExtractor(
            db_session,
            vision_extractor=vision,
            page_renderer=page_renderer,
            media_transcriber_fn=media_transcriber,
        )
        stage_a._extractors["audio"]._splitter_fn = lambda *_args, **_kwargs: [
            MediaChunk(
                index=0,
                start_ms=0,
                end_ms=60_000,
                payload_bytes=b"fake-audio",
                mime_type="audio/mpeg",
            )
        ]
        result = await stage_a.run(
            source_document_id=sd.id,
            source_path="/tmp/audio.mp3",
            source_type="audio",
            primary_language="bn",
        )

        assert result.calibration.path == "media_transcript"
        assert result.extraction_method_counts == {"transcript": 1}
        page_renderer.assert_not_called()
        vision.extract_page.assert_not_awaited()
        row = (
            await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == sd.id))
        ).scalar_one()
        assert row.extraction_method == "transcript"

    async def test_empty_transcript_fails_stage_1(self, db_session: AsyncSession) -> None:
        sd = SourceDocument(
            title="silent-audio",
            source_type="audio",
            primary_language="bn",
            content_domain="clinical",
            original_storage_path="/tmp/silent.mp3",
        )
        db_session.add(sd)
        await db_session.flush()
        await db_session.commit()

        media_transcriber = AsyncMock(return_value="   ")
        stage_a = StageAExtractor(
            db_session,
            media_transcriber_fn=media_transcriber,
        )
        stage_a._extractors["audio"]._splitter_fn = lambda *_args, **_kwargs: [
            MediaChunk(
                index=0,
                start_ms=0,
                end_ms=60_000,
                payload_bytes=b"fake-audio",
                mime_type="audio/mpeg",
            )
        ]
        with pytest.raises(Stage1DocumentEmptyError, match="The document is empty"):
            await stage_a.run(
                source_document_id=sd.id,
                source_path="/tmp/silent.mp3",
                source_type="audio",
                primary_language="bn",
            )

        await db_session.rollback()
        refreshed = (
            await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd.id))
        ).scalar_one()
        assert refreshed.status == "failed"


# ─── Empty outline is acceptable (post-c5b6635 chunker contract) ─────────


class TestOutlineEmptyIsAcceptable:
    """Under the token-budget chunker (c5b6635), the outline is supplementary
    context for the identifier — a hint for preferred chunk boundaries —
    rather than a load-bearing anchor. Empty outline must NOT fail Stage 1;
    the identifier reads body content directly.

    The pre-c5b6635 contract that raised `Stage1ExtractionError(outline_empty)`
    was right for the outline-partitioner era and obsolete now. Surfaced
    on the ASHA-NCDs-Hindi ingest where PyMuPDF text extracts cleanly but
    has no `#` markers — clean text with structure invisible to the parser
    is a real document shape, not a quality failure.
    """

    async def test_no_heading_markers_anywhere_succeeds_with_warning(
        self, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ) -> None:
        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=False) for i in (1, 2, 3)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=len(text_pages),
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            with caplog.at_level(
                "WARNING", logger="platform_service.workers.extractors.stage_a_outline_assembler"
            ):
                await stage_a.run(
                    source_document_id=sd_id,
                    source_path="/tmp/x.pdf",
                    source_type="pdf",
                    primary_language="en",
                )

        # Warning was emitted but the stage didn't raise.
        assert any(
            "outline empty" in rec.message and "identifier will run on body content only" in rec.message
            for rec in caplog.records
        ), "Empty outline must produce a warning explaining the consequence"

    async def test_pages_persist_when_outline_empty(self, db_session: AsyncSession) -> None:
        """Pages must be durably committed even on the empty-outline path —
        a re-affirmation of the smoke-loop commit-before-raise invariant.
        Even though Stage 1 no longer raises here, the commit ordering
        still matters for any future failure between the per-page loop
        and the orchestrator's session lifecycle.
        """
        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=False) for i in (1, 2, 3)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=len(text_pages),
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        await db_session.rollback()
        rows = (
            (await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == sd_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 3

    async def test_outline_jsonb_persisted_even_when_empty(self, db_session: AsyncSession) -> None:
        """outline_jsonb is committed (with empty sections list) so the
        dashboard's run-detail can show what Stage 1 saw."""
        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(1, has_heading=False)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=1,
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        await db_session.rollback()
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        assert sd.outline_method == "markdown_parser"
        assert sd.outline_jsonb is not None
        assert sd.outline_jsonb.get("sections") == []


# ─── Outline assembly happy path ──────────────────────────────────────────


class TestOutlineAssembledFromHeadings:
    async def test_text_path_with_headings_persists_outline(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        text_pages = [
            ExtractedPage(
                page_number=i,
                markdown=f"# Chapter {i}\n\nBody content for page {i} with several words.",
            )
            for i in (1, 2, 3)
        ]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=3,
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # Stage 1 returned a result with a non-zero outline_section_count.
        assert result.outline_section_count >= 1
        assert result.pages_persisted == 3
        # Source document has outline_jsonb populated with at least one section.
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        sections = (sd.outline_jsonb or {}).get("sections", [])
        assert len(sections) >= 1


# ─── Calibration → text_only path ─────────────────────────────────────────


class TestCalibrationTextOnly:
    async def test_clean_text_takes_text_only_path_no_vision_calls(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        # All pages have clean English text → calibration picks text_only.
        text_pages = [_good_text_page(i, has_heading=True) for i in range(1, 11)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=10,
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # All pages routed through text path; no vision calls.
        assert result.extraction_method_counts.get("text", 0) == 10
        assert result.extraction_method_counts.get("vision", 0) == 0
        # Verify per-row.
        rows = (
            (await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == sd_id)))
            .scalars()
            .all()
        )
        assert all(p.extraction_method == "text" for p in rows)


# ─── Calibration → all_vision path ────────────────────────────────────────


class TestCalibrationAllVision:
    async def test_force_vision_threshold_routes_every_page_through_vision(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force `decide_path` to "all_vision" by lowering the force threshold
        # below any non-negative fail rate.
        settings = get_settings()
        monkeypatch.setattr(settings, "extraction_calibration_force_vision_threshold", -1.0)
        monkeypatch.setattr(settings, "extraction_calibration_skip_vision_threshold", -1.0)
        # Clear the get_settings cache so the next get_settings() re-reads the
        # patched fields. (lru_cache stores the original Settings instance —
        # since we patched an attribute of it, the change takes effect on
        # the cached instance directly.)

        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2, 3)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=3,
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # Every page extraction_method='vision'.
        assert result.extraction_method_counts.get("vision", 0) == 3
        assert result.extraction_method_counts.get("text", 0) == 0


# ─── Empty / below-threshold document text fails Stage 1 ─────────────────


class TestDocumentEmptyFails:
    async def test_all_blank_pages_raise_and_mark_failed(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        text_pages = [
            ExtractedPage(page_number=1, markdown=""),
            ExtractedPage(page_number=2, markdown="   \n\t  "),
        ]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=len(text_pages),
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages, vision_markdown="")
            with pytest.raises(Stage1DocumentEmptyError, match="The document is empty"):
                await stage_a.run(
                    source_document_id=sd_id,
                    source_path="/tmp/x.pdf",
                    source_type="pdf",
                    primary_language="en",
                )

        await db_session.rollback()
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        assert sd.status == "failed"

    async def test_below_threshold_total_raises(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        min_chars = get_settings().extraction_quality_text_empty_min_chars
        short = "x" * (min_chars - 1)
        text_pages = [ExtractedPage(page_number=1, markdown=short)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=1,
        ):
            # Force text_only so vision cannot inflate the char count.
            stage_a = _make_stage_a(db_session, text_pages=text_pages)
            with (
                patch(
                    "platform_service.workers.extractors.stage_a_document_path.sample_calibration_for_document",
                ) as calib,
                pytest.raises(Stage1DocumentEmptyError),
            ):
                calib.return_value = (
                    CalibrationDecision(
                        path="text_only",
                        sample_pages_evaluated=[1],
                        sample_pass_count=1,
                        sample_fail_count=0,
                        sample_fail_rate=0.0,
                    ),
                    {},
                )
                await stage_a.run(
                    source_document_id=sd_id,
                    source_path="/tmp/x.pdf",
                    source_type="pdf",
                    primary_language="en",
                )

        await db_session.rollback()
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        assert sd.status == "failed"

    async def test_vision_recovery_above_threshold_succeeds(self, db_session: AsyncSession) -> None:
        """Empty text pages that vision recovers with enough text must pass."""
        sd_id = await _seed_source_document(db_session)
        text_pages = [ExtractedPage(page_number=1, markdown="")]
        recovered = "# Recovered\n\n" + ("body " * 20)

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=1,
        ):
            stage_a = _make_stage_a(db_session, text_pages=text_pages, vision_markdown=recovered)
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        assert result.pages_persisted == 1
        await db_session.rollback()
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        assert sd.status == "ingested"


# ─── Zero-page document ───────────────────────────────────────────────────


class TestZeroPageDocument:
    async def test_zero_pages_raises_document_empty(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=0,
        ):
            stage_a = _make_stage_a(db_session, text_pages=[])
            with pytest.raises(Stage1DocumentEmptyError, match="The document is empty"):
                await stage_a.run(
                    source_document_id=sd_id,
                    source_path="/tmp/x.pdf",
                    source_type="pdf",
                    primary_language="en",
                )

        await db_session.rollback()
        sd = (await db_session.execute(select(SourceDocument).where(SourceDocument.id == sd_id))).scalar_one()
        assert sd.status == "failed"


# ─── Vision recovery pass ────────────────────────────────────────────────


def _make_stage_a_with_recovery_vision(
    session: AsyncSession,
    *,
    text_pages: list[ExtractedPage],
    main_loop_failure_pages: set[int],
    recovery_succeeds_for: set[int] | None = None,
) -> StageAExtractor:
    """Build a StageAExtractor whose vision mock fails on `main_loop_failure_pages`
    during the main loop, and (optionally) succeeds for `recovery_succeeds_for`
    when retried in the recovery pass.

    The `recovery_succeeds_for` set is checked on attempt N≥2 (i.e. after
    the main loop) so the same vision mock simulates "first call fails,
    later call succeeds" behaviour.
    """
    if recovery_succeeds_for is None:
        recovery_succeeds_for = set()

    text_extractor_fn = MagicMock(return_value=text_pages)
    page_renderer = MagicMock(return_value=b"FAKE_PNG")

    call_counts: dict[int, int] = {}

    async def _vision_side_effect(*, page_image_bytes, mime_type, page_label, trace_context=None):
        # page_label format is "{doc_id}/page_{n}" — extract n.
        page_n = int(page_label.split("page_")[-1])
        call_counts[page_n] = call_counts.get(page_n, 0) + 1
        attempt = call_counts[page_n]

        if page_n in main_loop_failure_pages and attempt == 1:
            raise VisionExtractionError(f"simulated main-loop failure for page {page_n}")
        if page_n in main_loop_failure_pages and attempt > 1:
            if page_n in recovery_succeeds_for:
                return VisionExtractionResult(
                    markdown=f"# Recovered page {page_n}\n\nFresh content.",
                    raw_response=MagicMock(),
                )
            raise VisionExtractionError(f"simulated recovery failure for page {page_n}")
        return VisionExtractionResult(
            markdown=f"# Page {page_n}\n\nMain-loop content.",
            raw_response=MagicMock(),
        )

    vision = MagicMock()
    vision.extract_page = AsyncMock(side_effect=_vision_side_effect)

    return StageAExtractor(
        session,
        vision_extractor=vision,
        page_renderer=page_renderer,
        text_extractor_fn=text_extractor_fn,
    )


@pytest_asyncio.fixture
async def _fast_recovery_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the wall-clock waits in the recovery pass (initial 60s + per-attempt
    backoffs). We patch asyncio.sleep in the vision-recovery module so only the
    recovery code's sleeps get neutralised — production would still wait the
    configured durations."""

    async def _no_sleep(_seconds: float) -> None:  # noqa: D401
        return None

    monkeypatch.setattr(
        "platform_service.workers.extractors.stage_a_vision_recovery.asyncio.sleep",
        _no_sleep,
    )


def _force_all_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every page route through the vision path via calibration."""
    settings = get_settings()
    monkeypatch.setattr(settings, "extraction_calibration_force_vision_threshold", -1.0)
    monkeypatch.setattr(settings, "extraction_calibration_skip_vision_threshold", -1.0)


class TestVisionRecoveryPass:
    """The recovery pass retries vision on pages that landed as
    `vision_failed` in the main loop. Tolerance check enforces that the
    final residual count is within budget — strict 0 by default."""

    async def test_recovery_succeeds_upgrades_row_to_vision(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        _fast_recovery_settings: None,
    ) -> None:
        _force_all_vision(monkeypatch)
        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2, 3)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=3,
        ):
            stage_a = _make_stage_a_with_recovery_vision(
                db_session,
                text_pages=text_pages,
                main_loop_failure_pages={2},
                recovery_succeeds_for={2},
            )
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # All three pages now method='vision' (page 2 recovered).
        assert result.extraction_method_counts.get("vision", 0) == 3
        assert result.extraction_method_counts.get("vision_failed", 0) == 0
        # Verify per-row.
        rows = (
            (await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == sd_id)))
            .scalars()
            .all()
        )
        assert all(p.extraction_method == "vision" for p in rows)
        # Page 2's content is the recovered markdown, not the main-loop text.
        page_2 = next(p for p in rows if p.page_number == 2)
        assert "Recovered page 2" in page_2.markdown_content

    async def test_recovery_failure_raises_with_failing_page_numbers(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        _fast_recovery_settings: None,
    ) -> None:
        """When recovery cannot rescue a page AND tolerance=0 (the default),
        Stage 1 raises Stage1RecoveryFailedError with the page numbers that
        remain in vision_failed. Operator can then raise quota / tolerance."""
        _force_all_vision(monkeypatch)
        settings = get_settings()
        # Keep tolerance strict (default 0).
        # Drop max_retries to 1 so the test runs quickly.
        monkeypatch.setattr(settings, "stage_a_vision_recovery_max_retries", 1)

        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2, 3, 4)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=4,
        ):
            stage_a = _make_stage_a_with_recovery_vision(
                db_session,
                text_pages=text_pages,
                main_loop_failure_pages={2, 4},
                recovery_succeeds_for=set(),  # neither recovers
            )
            with pytest.raises(Stage1RecoveryFailedError) as exc_info:
                await stage_a.run(
                    source_document_id=sd_id,
                    source_path="/tmp/x.pdf",
                    source_type="pdf",
                    primary_language="en",
                )

        assert sorted(exc_info.value.failed_page_numbers) == [2, 4]
        assert exc_info.value.tolerance == 0

    async def test_tolerance_above_zero_allows_residual_failures(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        _fast_recovery_settings: None,
    ) -> None:
        """With tolerance=2, two unrecovered pages do NOT raise — Stage 1
        completes. The pages stay marked vision_failed in the DB so the
        dashboard can flag them, but the run is allowed to proceed."""
        _force_all_vision(monkeypatch)
        settings = get_settings()
        monkeypatch.setattr(settings, "stage_a_vision_failed_tolerance", 2)
        monkeypatch.setattr(settings, "stage_a_vision_recovery_max_retries", 1)

        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2, 3, 4)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=4,
        ):
            stage_a = _make_stage_a_with_recovery_vision(
                db_session,
                text_pages=text_pages,
                main_loop_failure_pages={2, 4},
                recovery_succeeds_for=set(),
            )
            # Recovery cannot rescue, but tolerance=2 → no raise.
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        assert result.extraction_method_counts.get("vision_failed", 0) == 2
        assert result.extraction_method_counts.get("vision", 0) == 2

    async def test_no_failures_main_loop_skips_recovery_no_extra_calls(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        _fast_recovery_settings: None,
    ) -> None:
        """When the main loop produced no vision_failed rows, the recovery
        pass is a no-op — no extra LLM calls, no sleeps, completes fast."""
        _force_all_vision(monkeypatch)
        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=2,
        ):
            stage_a = _make_stage_a_with_recovery_vision(
                db_session,
                text_pages=text_pages,
                main_loop_failure_pages=set(),
            )
            result = await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # Vision called once per page in main loop; no recovery retries.
        assert stage_a._vision.extract_page.await_count == 2
        assert result.extraction_method_counts.get("vision", 0) == 2

    async def test_partial_recovery_only_failures_retried(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
        _fast_recovery_settings: None,
    ) -> None:
        """Recovery pass iterates only over vision_failed rows — pages that
        succeeded in the main loop are not re-extracted (would be wasteful
        + would clobber fresh content with a redundant call)."""
        _force_all_vision(monkeypatch)
        settings = get_settings()
        monkeypatch.setattr(settings, "stage_a_vision_recovery_max_retries", 1)

        sd_id = await _seed_source_document(db_session)
        text_pages = [_good_text_page(i, has_heading=True) for i in (1, 2, 3)]

        with patch(
            "platform_service.workers.stage_a_extract.count_pages",
            return_value=3,
        ):
            stage_a = _make_stage_a_with_recovery_vision(
                db_session,
                text_pages=text_pages,
                main_loop_failure_pages={3},
                recovery_succeeds_for={3},
            )
            await stage_a.run(
                source_document_id=sd_id,
                source_path="/tmp/x.pdf",
                source_type="pdf",
                primary_language="en",
            )

        # Pages 1, 2 = 1 call each (main loop). Page 3 = 1 main-loop fail
        # + 1 recovery success = 2 calls. Total = 4.
        assert stage_a._vision.extract_page.await_count == 4
