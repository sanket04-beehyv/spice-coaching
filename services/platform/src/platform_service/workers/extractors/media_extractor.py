"""Chunked AV source extractor (audio / video).

Replaces the prior single-call inline-Gemini implementation. The
orchestrator-side splitter cuts the source into ~2-minute windows
before any LLM call; the splitter owns the timecodes. Each chunk
becomes one ``ExtractedPage`` with ``start_ms`` / ``end_ms`` drawn
from the chunk boundaries — the model never names a time, so it
cannot hallucinate one.

Output ``requires_calibration=False`` — transcripts are already LLM
output, so the Stage A orchestrator persists them directly without
running them through the document-text calibration / vision-fallback
decision.

Test injection:
- ``splitter_fn`` — replace the ffmpeg-backed splitter with a stub
  that returns deterministic chunks (no subprocess needed).
- ``transcribe_fn`` — replace the per-chunk transcription call with
  a stub returning synthetic transcript strings.
- ``ai_client`` — used when ``transcribe_fn`` is omitted.
"""

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.workers.extractors.base import SourceExtractionResult, SourceExtractor
from platform_service.workers.extractors.media_splitter import (
    MediaChunk,
    MediaSplitterError,
    split_into_chunks,
)
from platform_service.workers.extractors.text_extractor import ExtractedPage, TextExtractionError
from platform_service.workers.extractors.transcript_quality import compute_transcript_quality

logger = logging.getLogger(__name__)

SplitterFn = Callable[..., list[MediaChunk]]
TranscribeChunkFn = Callable[[bytes, str], Awaitable[str]]


class MediaSourceExtractor(SourceExtractor):
    supported_types: frozenset[str] = frozenset({"audio", "video"})

    def __init__(
        self,
        ai_client: AIRuntimeClient | None = None,
        splitter_fn: SplitterFn | None = None,
        transcribe_fn: TranscribeChunkFn | None = None,
    ) -> None:
        self._ai = ai_client
        self._splitter_fn = splitter_fn or split_into_chunks
        # transcribe_fn signature is (bytes, mime_type) -> awaitable[str], matching
        # AIRuntimeClient.transcribe_media. None defers to the ai_client.
        self._transcribe_fn = transcribe_fn

    async def _transcribe(self, payload: bytes, mime_type: str) -> str:
        if self._transcribe_fn is not None:
            return await self._transcribe_fn(payload, mime_type)
        if self._ai is None:
            raise TextExtractionError(
                "MediaSourceExtractor requires ai_client or transcribe_fn; "
                "do not construct without a shared AIRuntimeClient"
            )
        return await self._ai.transcribe_media(payload, mime_type)

    async def extract(
        self,
        source_path: str | Path,
        *,
        source_type: str,
        primary_language: str,
    ) -> SourceExtractionResult:
        try:
            chunks = await anyio.to_thread.run_sync(
                lambda: self._splitter_fn(source_path, source_type=source_type)
            )
        except MediaSplitterError as exc:
            raise TextExtractionError(str(exc)) from exc

        if not chunks:
            raise TextExtractionError(f"media splitter produced no chunks for {source_path!s}")

        pages: list[ExtractedPage] = []
        for chunk in chunks:
            text = await self._transcribe(chunk.payload_bytes, chunk.mime_type)
            cleaned = text.strip()
            if cleaned:
                markdown = f"# Transcript (chunk {chunk.index + 1})\n\n{cleaned}"
            else:
                markdown = ""
            pages.append(
                ExtractedPage(
                    page_number=chunk.index + 1,
                    markdown=markdown,
                    start_ms=chunk.start_ms,
                    end_ms=chunk.end_ms,
                    extraction_quality_score=compute_transcript_quality(cleaned),
                    language_detected=None,  # TODO(media-lang-detect): extend ai-runtime contract.
                )
            )
        return SourceExtractionResult(
            pages=pages,
            requires_calibration=False,
            extraction_method_label="transcript",
        )
