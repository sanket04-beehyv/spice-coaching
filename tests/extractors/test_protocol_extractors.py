"""Unit tests for the Stage 1 source-extractor protocol implementations."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from platform_service.workers.extractors.base import (
    SourceExtractionResult,
    SourceExtractor,
)
from platform_service.workers.extractors.document_extractor import DocumentSourceExtractor
from platform_service.workers.extractors.media_extractor import MediaSourceExtractor
from platform_service.workers.extractors.media_splitter import MediaChunk
from platform_service.workers.extractors.text_extractor import ExtractedPage, TextExtractionError
from platform_service.workers.stage_a_extract import StageAExtractor

pytestmark = pytest.mark.asyncio


# ─── DocumentSourceExtractor ────────────────────────────────────────────


class TestDocumentSourceExtractor:
    async def test_supports_pdf_pptx_docx(self) -> None:
        ext = DocumentSourceExtractor()
        assert ext.supported_types == frozenset({"pdf", "pptx", "docx"})
        assert isinstance(ext, SourceExtractor)

    async def test_requires_calibration_is_true(self) -> None:
        fake_pages = [ExtractedPage(page_number=1, markdown="# Hello")]
        fake_fn = MagicMock(return_value=fake_pages)
        ext = DocumentSourceExtractor(extract_pages_fn=fake_fn)

        result = await ext.extract("/tmp/x.pdf", source_type="pdf", primary_language="bn")

        assert isinstance(result, SourceExtractionResult)
        assert result.pages == fake_pages
        assert result.requires_calibration is True
        assert result.extraction_method_label == "text"

    async def test_passes_path_and_source_type_through_to_fn(self) -> None:
        fake_fn = MagicMock(return_value=[])
        ext = DocumentSourceExtractor(extract_pages_fn=fake_fn)

        await ext.extract("/tmp/y.docx", source_type="docx", primary_language="en")

        fake_fn.assert_called_once_with("/tmp/y.docx", "docx")

    async def test_propagates_extraction_errors(self) -> None:
        fake_fn = MagicMock(side_effect=TextExtractionError("corrupt"))
        ext = DocumentSourceExtractor(extract_pages_fn=fake_fn)

        with pytest.raises(TextExtractionError):
            await ext.extract("/tmp/x.pdf", source_type="pdf", primary_language="bn")


# ─── MediaSourceExtractor ────────────────────────────────────────────────


class TestMediaSourceExtractor:
    async def test_supports_audio_video(self) -> None:
        ext = MediaSourceExtractor()
        assert ext.supported_types == frozenset({"audio", "video"})
        assert isinstance(ext, SourceExtractor)

    # Per-chunk extraction behaviour lives in
    # ``tests/extractors/test_media_chunked_extractor.py`` (timecodes, empty
    # transcripts, splitter errors). The splitter itself is covered by
    # ``test_media_splitter.py``.


# ─── Stage A orchestrator integration ────────────────────────────────────


async def test_orchestrator_default_registry_covers_all_supported_types() -> None:
    """The orchestrator's default registry should expose pdf, pptx, docx, audio, video."""
    fake_session = MagicMock()
    orchestrator = StageAExtractor(fake_session)

    assert set(orchestrator._extractors.keys()) == {"pdf", "pptx", "docx", "audio", "video"}
    # PDF/PPTX/DOCX share one DocumentSourceExtractor instance.
    assert orchestrator._extractors["pdf"] is orchestrator._extractors["pptx"]
    assert orchestrator._extractors["pdf"] is orchestrator._extractors["docx"]
    # AV share one MediaSourceExtractor instance.
    assert orchestrator._extractors["audio"] is orchestrator._extractors["video"]
    assert orchestrator._extractors["pdf"] is not orchestrator._extractors["audio"]


async def test_orchestrator_uses_injected_text_extractor_fn() -> None:
    """Backward-compat: passing ``text_extractor_fn`` should reach the document wrapper."""
    fake_session = MagicMock()
    sentinel_pages = [ExtractedPage(page_number=1, markdown="injected")]
    fake_text_fn = MagicMock(return_value=sentinel_pages)
    orchestrator = StageAExtractor(fake_session, text_extractor_fn=fake_text_fn)

    result = await orchestrator._extractors["pdf"].extract(
        "/tmp/x.pdf", source_type="pdf", primary_language="bn"
    )
    assert result.pages == sentinel_pages
    fake_text_fn.assert_called_once()


async def test_orchestrator_uses_injected_media_transcriber_fn() -> None:
    """Orchestrator-level ``media_transcriber_fn`` hook reaches the chunked
    extractor's per-chunk transcribe call (signature: ``(bytes, mime_type) -> str``)."""
    fake_session = MagicMock()
    fake_transcribe = AsyncMock(return_value="some transcribed text from the chunk")
    orchestrator = StageAExtractor(fake_session, media_transcriber_fn=fake_transcribe)

    # Inject a stub splitter on the constructed media extractor so we don't
    # need ffmpeg present for this test.
    media_ext = orchestrator._extractors["audio"]
    media_ext._splitter_fn = lambda *_args, **_kwargs: [
        MediaChunk(
            index=0,
            start_ms=0,
            end_ms=60_000,
            payload_bytes=b"fake",
            mime_type="audio/mpeg",
        )
    ]

    result = await media_ext.extract("/tmp/y.mp3", source_type="audio", primary_language="bn")
    assert len(result.pages) == 1
    fake_transcribe.assert_awaited_once_with(b"fake", "audio/mpeg")
