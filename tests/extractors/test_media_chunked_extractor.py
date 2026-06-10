"""Unit tests for the chunked AV ``MediaSourceExtractor``."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from platform_service.workers.extractors.media_extractor import MediaSourceExtractor
from platform_service.workers.extractors.media_splitter import MediaChunk, MediaSplitterError
from platform_service.workers.extractors.text_extractor import TextExtractionError

pytestmark = pytest.mark.asyncio


def _chunk(index: int, start_ms: int, end_ms: int, payload: bytes = b"x") -> MediaChunk:
    return MediaChunk(
        index=index,
        start_ms=start_ms,
        end_ms=end_ms,
        payload_bytes=payload,
        mime_type="audio/mpeg",
    )


async def test_one_chunk_one_extracted_page_with_timecodes() -> None:
    splitter = MagicMock(return_value=[_chunk(0, 0, 30_000)])
    transcribe = AsyncMock(return_value="Hello world this is a sentence with enough words")
    ext = MediaSourceExtractor(splitter_fn=splitter, transcribe_fn=transcribe)

    result = await ext.extract("/tmp/x.mp3", source_type="audio", primary_language="bn")

    assert result.requires_calibration is False
    assert result.extraction_method_label == "transcript"
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.page_number == 1
    assert page.start_ms == 0
    assert page.end_ms == 30_000
    assert "Hello world" in page.markdown
    assert page.markdown.startswith("# Transcript (chunk 1)")
    # Quality must be non-trivial for a >5-word transcript.
    assert page.extraction_quality_score is not None
    assert page.extraction_quality_score >= 0.5


async def test_many_chunks_produce_sequential_pages() -> None:
    splitter = MagicMock(
        return_value=[
            _chunk(0, 0, 120_000),
            _chunk(1, 105_000, 225_000),
            _chunk(2, 210_000, 300_000),
        ]
    )
    transcribe = AsyncMock(return_value="A reasonably long transcript with several words present.")
    ext = MediaSourceExtractor(splitter_fn=splitter, transcribe_fn=transcribe)

    result = await ext.extract("/tmp/v.mp4", source_type="video", primary_language="bn")

    assert [p.page_number for p in result.pages] == [1, 2, 3]
    assert [(p.start_ms, p.end_ms) for p in result.pages] == [
        (0, 120_000),
        (105_000, 225_000),
        (210_000, 300_000),
    ]
    assert transcribe.await_count == 3


async def test_empty_transcript_records_empty_page_with_zero_quality() -> None:
    """A chunk that came back as silence should land as a low-quality page,
    not crash the run — the downstream insufficient_source_filter handles it."""
    splitter = MagicMock(return_value=[_chunk(0, 0, 60_000)])
    transcribe = AsyncMock(return_value="   ")
    ext = MediaSourceExtractor(splitter_fn=splitter, transcribe_fn=transcribe)

    result = await ext.extract("/tmp/x.mp3", source_type="audio", primary_language="bn")

    page = result.pages[0]
    assert page.markdown == ""
    assert page.extraction_quality_score == 0.0


async def test_splitter_error_becomes_text_extraction_error() -> None:
    def boom(*args, **kwargs):
        raise MediaSplitterError("ffmpeg not available")

    ext = MediaSourceExtractor(splitter_fn=boom, transcribe_fn=AsyncMock())
    with pytest.raises(TextExtractionError, match="ffmpeg not available"):
        await ext.extract("/tmp/x.mp3", source_type="audio", primary_language="bn")


async def test_empty_chunk_list_raises_text_extraction_error() -> None:
    splitter = MagicMock(return_value=[])
    ext = MediaSourceExtractor(splitter_fn=splitter, transcribe_fn=AsyncMock())
    with pytest.raises(TextExtractionError, match="no chunks"):
        await ext.extract("/tmp/x.mp3", source_type="audio", primary_language="bn")


async def test_transcribe_fn_receives_chunk_bytes_and_mime_type() -> None:
    splitter = MagicMock(return_value=[_chunk(0, 0, 30_000, payload=b"chunk-bytes")])
    transcribe = AsyncMock(return_value="some text")
    ext = MediaSourceExtractor(splitter_fn=splitter, transcribe_fn=transcribe)

    await ext.extract("/tmp/x.mp3", source_type="audio", primary_language="bn")

    transcribe.assert_awaited_once_with(b"chunk-bytes", "audio/mpeg")
