"""Layer 3 — AV E2E: chunked transcription produces modules with timecode attribution.

Validates the source-attribution-and-av PR series end to end against
a real Postgres:

- Stage A's chunked ``MediaSourceExtractor`` with mocked splitter +
  mocked per-chunk transcribe (no ffmpeg, no Gemini).
- Stage C parses ``content_block`` rows from transcript markdown
  without source_type gating.
- Stage D drafts modules with ``source_block_ids`` referencing those
  blocks and ``module.source_document_ids`` populated.
- ``source_page`` rows carry the deterministic ``start_ms`` / ``end_ms``
  the splitter assigned — the model never named a time.

Together with ``test_pipeline_e2e.TestHappyPath``, this proves that
both attribution use cases (PDF page numbers, AV timecodes) hang
together end-to-end on top of Deepak's pipeline.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from platform_service.db.base import SessionLocal
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.module import Module
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.services.module_identifier import ModuleIdentifier
from platform_service.workers.extractors.document_extractor import DocumentSourceExtractor
from platform_service.workers.extractors.media_extractor import MediaSourceExtractor
from platform_service.workers.extractors.media_splitter import MediaChunk
from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator
from platform_service.workers.stage_a_extract import StageAExtractor
from platform_service.workers.stage_c_identify import StageCOrchestrator
from platform_service.workers.stage_d_draft import StageDOrchestrator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.integration.test_pipeline_e2e import (  # noqa: F401 — pytest fixtures imported by name
    _draft_response,
    _id_response,
    _stub_post_publish_enqueue,  # autouse fixture
    _wipe_data_between_tests,  # autouse fixture
)

# ``patch_count_pages`` lives in tests/integration/conftest.py so pytest
# discovers it without a cross-test-file import (which would trip F811).

pytestmark = [requires_db]


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_av_chunks(n: int, *, duration_ms: int = 120_000, overlap_ms: int = 15_000) -> list[MediaChunk]:
    """Synthetic chunks with the same timecode math the real splitter uses."""
    step_ms = duration_ms - overlap_ms
    return [
        MediaChunk(
            index=i,
            start_ms=i * step_ms,
            end_ms=i * step_ms + duration_ms,
            payload_bytes=f"chunk-{i}-bytes".encode(),
            mime_type="audio/mpeg",
        )
        for i in range(n)
    ]


def _make_transcript_text(chunk_index: int) -> str:
    """Markdown-shaped transcript so the content_block_parser produces real blocks."""
    return (
        f"This is the transcript for chunk {chunk_index}. "
        f"Detailed clinical discussion of hypertension management and patient counselling. "
        f"Coverage of referral thresholds and follow-up cadence. " * 3
    )


async def _seed_av_source_doc(session: AsyncSession) -> UUID:
    sd = SourceDocument(
        title="av-integration-test",
        source_type="audio",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        # Local-shaped path — `materialize_local_source_file` returns it as-is
        # when it doesn't match the configured MinIO bucket prefix, so the
        # orchestrator runs without needing real object storage. The mocked
        # splitter ignores the path arg anyway.
        original_storage_path="/tmp/fake.mp3",
    )
    session.add(sd)
    await session.flush()
    await session.commit()
    return sd.id


def _id_candidate_for_source(
    block_ids: list[UUID], source_document_id: UUID, *, title: str = "Topic"
) -> dict[str, Any]:
    """Variant of ``_id_candidate`` that pins ``source_document_id`` to the
    seeded doc so Stage D's ``_extract_source_document_ids`` ends up with
    the real id (production identifier output points at real docs; the
    shared     shared fixture uses ``uuid4()`` for that field which is fine
    for the PDF test's weaker assertion but masks attribution drift)."""
    return {
        "proposed_title": title,
        "scope_summary": "Summary of the topic.",
        "source_provenance": [
            {
                "source_document_id": str(source_document_id),
                "source_page_id": str(uuid4()),
                "content_block_ids": [str(b) for b in block_ids],
            }
        ],
        "estimated_card_count": 5,
        "estimated_quiz_count": 4,
        "proposed_module_type": "refresher",
    }


async def _run_av_orchestrator(
    *,
    source_document_id: UUID,
    chunks: list[MediaChunk],
) -> Any:
    """PipelineOrchestrator with mocked splitter + per-chunk transcribe.

    Mirrors ``test_pipeline_e2e._run_orchestrator`` for the AV path:
    the chunked ``MediaSourceExtractor`` receives a stub splitter that
    returns the supplied chunks and a stub transcribe that emits
    deterministic markdown so Stage C's content_block parser can produce
    real blocks.
    """
    own_session = SessionLocal()
    try:
        splitter_fn = MagicMock(return_value=chunks)

        async def _transcribe(payload: bytes, _mime_type: str) -> str:
            idx = int(payload.decode().split("-")[1])
            return _make_transcript_text(idx)

        media_ext = MediaSourceExtractor(splitter_fn=splitter_fn, transcribe_fn=_transcribe)
        doc_ext = DocumentSourceExtractor()
        extractors = {
            "audio": media_ext,
            "video": media_ext,
            "pdf": doc_ext,
            "pptx": doc_ext,
            "docx": doc_ext,
        }

        vision_extractor = MagicMock()
        vision_extractor.extract_page = AsyncMock()
        stage_a = StageAExtractor(
            own_session,
            vision_extractor=vision_extractor,
            extractors=extractors,
        )

        identifier = MagicMock(spec=ModuleIdentifier)

        async def _identify(**kwargs: Any):
            valid_block_ids = list(kwargs.get("valid_block_ids") or [])
            # Pin source_document_id to the seeded doc so Stage D's
            # extraction lands the real id on ``module.source_document_ids``.
            cands = (
                [_id_candidate_for_source(valid_block_ids[:1], source_document_id)] if valid_block_ids else []
            )
            return _id_response(cands)

        identifier.identify = AsyncMock(side_effect=_identify)
        stage_c = StageCOrchestrator(own_session, identifier=identifier)

        card_drafter = MagicMock()
        card_drafter.draft = AsyncMock(return_value=_draft_response())
        stage_d = StageDOrchestrator(own_session, card_drafter=card_drafter)

        orch = PipelineOrchestrator(own_session, stage_a=stage_a, stage_c=stage_c, stage_d=stage_d)
        return await orch.run(
            source_document_id=source_document_id,
            source_path="/tmp/fake.mp3",
            source_type="audio",
            resume=True,
        )
    finally:
        await own_session.close()


# ─── Tests ─────────────────────────────────────────────────────────────────


class TestAvHappyPath:
    async def test_av_pipeline_produces_module_with_timecode_attribution(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_av_source_doc(db_session)
        chunks = _make_av_chunks(3)
        # count_pages for AV currently returns 1 (a stale leftover from the
        # single-page transcript era). Patch it to the chunk count so the
        # orchestrator's StageAResult reports the right total. The chunked
        # extractor itself produces N pages regardless of what count_pages
        # reports.
        patch_count_pages(len(chunks))

        result = await _run_av_orchestrator(source_document_id=sd_id, chunks=chunks)

        # Pipeline succeeded end to end.
        assert result.final_status == "succeeded"
        assert result.candidates_emitted >= 1
        assert result.drafts_produced >= 1

        # source_page: one per chunk, carrying deterministic timecodes
        # owned by the splitter (not the LLM).
        pages = (
            (
                await db_session.execute(
                    select(SourcePage)
                    .where(SourcePage.source_document_id == sd_id)
                    .order_by(SourcePage.page_number)
                )
            )
            .scalars()
            .all()
        )
        assert len(pages) == 3
        assert [p.start_ms for p in pages] == [0, 105_000, 210_000]
        assert [p.end_ms for p in pages] == [120_000, 225_000, 330_000]
        assert all(p.extraction_method == "transcript" for p in pages)
        # Real composite quality (not the 1.0 hardcode the old path used).
        assert all(p.extraction_quality_score is not None for p in pages)
        assert all(0.0 < p.extraction_quality_score <= 1.0 for p in pages)

        # content_block: Stage C's _ensure_content_blocks parses any page's
        # markdown — no source_type gating — so transcripts produce blocks
        # just like PDFs.
        blocks = (
            (
                await db_session.execute(
                    select(ContentBlock)
                    .join(SourcePage, ContentBlock.source_page_id == SourcePage.id)
                    .where(SourcePage.source_document_id == sd_id)
                )
            )
            .scalars()
            .all()
        )
        assert blocks, "Stage C must produce content_blocks from transcript markdown"

        # Stage D drafts the module as draft (gated-publish flow). The
        # attribution graph requires source_document_ids on the module and
        # source_block_ids on each card so /coaching/rag-query can join
        # through to source_page (timecodes) and source_document.
        modules = (
            (await db_session.execute(select(Module).where(Module.lifecycle_status == "draft")))
            .scalars()
            .all()
        )
        assert len(modules) >= 1
        m = modules[0]
        assert m.module_json is not None
        cards = m.module_json.get("cards", [])
        assert len(cards) >= 1
        assert all(card.get("source_block_ids") for card in cards), (
            "Every drafted card must carry source_block_ids for attribution"
        )
        assert m.source_document_ids, (
            "Module must record source_document_ids so RAG attribution joins through to docs"
        )
        assert sd_id in m.source_document_ids

    async def test_av_single_chunk_short_source_produces_one_page(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        """Source shorter than the chunk window → one page that spans the source."""
        sd_id = await _seed_av_source_doc(db_session)
        chunks = [
            MediaChunk(
                index=0,
                start_ms=0,
                end_ms=45_000,  # 45-second clip
                payload_bytes=b"chunk-0-bytes",
                mime_type="audio/mpeg",
            )
        ]
        patch_count_pages(1)

        result = await _run_av_orchestrator(source_document_id=sd_id, chunks=chunks)
        assert result.final_status == "succeeded"

        pages = (
            (await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == sd_id)))
            .scalars()
            .all()
        )
        assert len(pages) == 1
        assert pages[0].start_ms == 0
        assert pages[0].end_ms == 45_000
