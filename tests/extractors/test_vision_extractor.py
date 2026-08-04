"""W-2 Stage A — vision_extractor unit tests with mocked AIRuntimeClient.

Verifies the vision call is constructed correctly and that error paths
surface as VisionExtractionError.
"""

import base64
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceRequest, InferenceResponse, TraceContext
from platform_service.services.prompt_template_service import PromptTemplateService, RenderedPrompt
from platform_service.workers.extractors.vision_extractor import (
    VisionExtractionError,
    VisionExtractor,
    _unwrap_envelope,
)


def _make_mock_response(
    *, raw_text: str = "extracted markdown", error: str | None = None
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.VISION_EXTRACTION,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text=raw_text,
        parsed_json=None,
        latency_ms=200,
        error=error,
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.generate = AsyncMock(return_value=_make_mock_response())
    return client


@pytest.fixture(autouse=True)
def mock_vision_prompt_template(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _render(*_args: Any, **_kwargs: Any) -> RenderedPrompt:
        return RenderedPrompt(
            template_id="vision",
            template_version=1,
            prompt_template_id=uuid4(),
            resolved_system_prompt="Extract verbatim. Do not translate.",
            resolved_human_message="Extract the supplied page image.",
        )

    monkeypatch.setattr(PromptTemplateService, "render", _render)


class TestVisionExtractorRequest:
    @pytest.mark.asyncio
    async def test_returns_markdown_on_success(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        result = await extractor.extract_page(
            page_image_bytes=b"PNG_BYTES",
            mime_type="image/png",
            page_label="page_1",
        )
        assert result.markdown == "extracted markdown"
        assert result.raw_response.error is None

    @pytest.mark.asyncio
    async def test_request_uses_vision_extraction_generation_type(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        await extractor.extract_page(page_image_bytes=b"x", page_label="p")
        sent: InferenceRequest = mock_client.generate.call_args.args[0]
        assert sent.generation_type == GenerationType.VISION_EXTRACTION

    @pytest.mark.asyncio
    async def test_request_includes_image_attachment(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        png = b"\x89PNG\r\n\x1a\nfake"
        await extractor.extract_page(page_image_bytes=png, page_label="page_3")
        sent: InferenceRequest = mock_client.generate.call_args.args[0]
        assert len(sent.image_attachments) == 1
        att = sent.image_attachments[0]
        assert att.mime_type == "image/png"
        assert base64.b64decode(att.data_base64) == png
        assert att.label == "page_3"

    @pytest.mark.asyncio
    async def test_request_does_not_send_model_policy(self, mock_client: AsyncMock) -> None:
        """Model selection is owned by ai-runtime generation profiles."""
        extractor = VisionExtractor(client=mock_client)
        await extractor.extract_page(page_image_bytes=b"x")
        sent: InferenceRequest = mock_client.generate.call_args.args[0]
        assert not hasattr(sent, "model_policy") or getattr(sent, "model_policy", None) is None
        assert "model_policy" not in sent.model_dump()
        assert sent.generation_type == GenerationType.VISION_EXTRACTION

    @pytest.mark.asyncio
    async def test_request_passes_trace_context(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        tc = TraceContext(ingestion_run_id="run-1", source_document_id="doc-1")
        await extractor.extract_page(page_image_bytes=b"x", trace_context=tc)
        sent: InferenceRequest = mock_client.generate.call_args.args[0]
        assert sent.trace_context.ingestion_run_id == "run-1"
        assert sent.trace_context.source_document_id == "doc-1"

    @pytest.mark.asyncio
    async def test_prompt_is_verbatim_preserving(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        await extractor.extract_page(page_image_bytes=b"x")
        sent: InferenceRequest = mock_client.generate.call_args.args[0]
        # The system prompt forbids translation/paraphrase per Pipeline §4.3.
        assert "verbatim" in sent.prompt.resolved_system_prompt.lower()
        assert "do not translate" in sent.prompt.resolved_system_prompt.lower()


class TestVisionExtractorErrorPaths:
    @pytest.mark.asyncio
    async def test_empty_bytes_raises_immediately(self, mock_client: AsyncMock) -> None:
        extractor = VisionExtractor(client=mock_client)
        with pytest.raises(VisionExtractionError, match="empty"):
            await extractor.extract_page(page_image_bytes=b"")
        # Should not have called the client
        mock_client.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_error_response_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_make_mock_response(raw_text="", error="provider boom"))
        extractor = VisionExtractor(client=client)
        with pytest.raises(VisionExtractionError, match="provider boom"):
            await extractor.extract_page(page_image_bytes=b"x")

    @pytest.mark.asyncio
    async def test_empty_response_text_returns_empty_markdown(self) -> None:
        """A successful provider call with empty content is a *legitimate*
        sparse-page outcome (cover, divider, decorative) — not a failure.
        Treating it as failure was the silent-chapter-loss bug: pages that
        genuinely had nothing to extract were marked vision_failed and fell
        back to garbage Bijoy text from the upstream text extractor.
        """
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_make_mock_response(raw_text=""))
        extractor = VisionExtractor(client=client)
        result = await extractor.extract_page(page_image_bytes=b"x")
        assert result.markdown == ""
        assert result.raw_response.error is None

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_empty_markdown(self) -> None:
        """Same as above — `_unwrap_envelope` strips outer whitespace, so
        a whitespace-only LLM response collapses to empty. Not a failure."""
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_make_mock_response(raw_text="   \n\n   "))
        extractor = VisionExtractor(client=client)
        result = await extractor.extract_page(page_image_bytes=b"x")
        assert result.markdown == ""


class TestArbitraryAdaptersStillWork:
    """Sanity check: any object with a generate(InferenceRequest) async method works."""

    @pytest.mark.asyncio
    async def test_custom_client_object(self) -> None:
        captured: dict[str, Any] = {}

        class _ClientStub:
            async def generate(self, req: InferenceRequest) -> InferenceResponse:
                captured["req"] = req
                return _make_mock_response(raw_text="from stub")

        extractor = VisionExtractor(client=_ClientStub())  # type: ignore[arg-type]
        result = await extractor.extract_page(page_image_bytes=b"x")
        assert result.markdown == "from stub"
        assert captured["req"].generation_type == GenerationType.VISION_EXTRACTION


class TestVisionHtmlNormalization:
    @pytest.mark.asyncio
    async def test_extract_normalizes_html_markup(self) -> None:
        html_body = "<b>Bold heading</b><br><ul><li>First item</li><li>Second item</li></ul>"
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_make_mock_response(raw_text=html_body))
        extractor = VisionExtractor(client=client)
        result = await extractor.extract_page(page_image_bytes=b"x", page_label="page_1")
        assert "<" not in result.markdown
        assert "**Bold heading**" in result.markdown
        assert "- First item" in result.markdown
        assert "- Second item" in result.markdown


# ─── JSON envelope unwrap (Layer 2 — regression for the bug we caught) ─────
#
# Gemini 2.5-flash sometimes wraps markdown output in a JSON envelope even
# when the prompt asks for plain markdown. The smoke loop showed
# `{"page_content": ["# Heading", "body para"]}` shapes coming back. Without
# unwrapping, downstream `markdown_outline_parser` finds zero `#`-prefixed
# lines and Stage 1 fails on outline_empty. The `_unwrap_envelope` helper
# is a defensive normalisation layer over the response.


class TestUnwrapEnvelopePure:
    """Pure-function tests for `_unwrap_envelope`. No mocks needed."""

    def test_plain_markdown_returned_unchanged(self) -> None:
        raw = "# Heading\n\nBody paragraph"
        assert _unwrap_envelope(raw) == "# Heading\n\nBody paragraph"

    def test_strips_outer_whitespace(self) -> None:
        assert _unwrap_envelope("  \n# H\nbody  \n  ") == "# H\nbody"

    def test_returns_empty_for_empty_string(self) -> None:
        assert _unwrap_envelope("") == ""

    def test_returns_empty_for_whitespace_only(self) -> None:
        assert _unwrap_envelope("   \n  \t ") == ""

    def test_unwrap_page_content_array(self) -> None:
        raw = '{"page_content": ["# Heading", "Body paragraph", "More body"]}'
        out = _unwrap_envelope(raw)
        assert out == "# Heading\n\nBody paragraph\n\nMore body"

    @pytest.mark.parametrize("key", ["page_content", "content", "markdown", "text", "body", "page_text"])
    def test_unwrap_known_keys_with_array_value(self, key: str) -> None:
        raw = f'{{"{key}": ["# H", "body"]}}'
        assert _unwrap_envelope(raw) == "# H\n\nbody"

    @pytest.mark.parametrize("key", ["page_content", "content", "markdown", "text", "body", "page_text"])
    def test_unwrap_known_keys_with_string_value(self, key: str) -> None:
        raw = f'{{"{key}": "# H\\nbody"}}'
        assert _unwrap_envelope(raw) == "# H\nbody"

    def test_unwrap_strips_blank_lines_in_array(self) -> None:
        raw = '{"page_content": ["# H", "", "  ", "body"]}'
        # Empty/whitespace-only entries should be dropped before joining.
        assert _unwrap_envelope(raw) == "# H\n\nbody"

    def test_unknown_envelope_key_falls_through_to_raw(self) -> None:
        raw = '{"weirdly_named": ["# H"]}'
        # Unknown shape — we surface SOMETHING (the raw JSON) rather than
        # silently emptying the page. A warning is logged.
        assert _unwrap_envelope(raw) == raw

    def test_non_dict_json_returned_as_raw(self) -> None:
        raw = '"# H, body"'  # Valid JSON but a string, not a dict
        assert _unwrap_envelope(raw) == raw

    def test_malformed_json_returned_as_raw(self) -> None:
        # The LLM sometimes emits text that LOOKS like JSON but isn't valid.
        # We must not crash; fall through to raw.
        raw = "{not actually json"
        assert _unwrap_envelope(raw) == raw

    def test_strips_markdown_code_fence(self) -> None:
        raw = "```markdown\n# H\n\nbody\n```"
        assert _unwrap_envelope(raw) == "# H\n\nbody"

    def test_strips_unlabeled_code_fence(self) -> None:
        raw = "```\n# H\nbody\n```"
        assert _unwrap_envelope(raw) == "# H\nbody"

    def test_strips_code_fence_around_json_envelope(self) -> None:
        # Worst case: fenced JSON wrapping the markdown. Both layers stripped.
        raw = '```json\n{"page_content": ["# H", "body"]}\n```'
        assert _unwrap_envelope(raw) == "# H\n\nbody"

    def test_first_known_key_wins_when_multiple(self) -> None:
        # If both `page_content` and `content` are present, the order of
        # _ENVELOPE_KEYS in the source picks the first; we just assert one
        # of them was used (not both joined together).
        raw = '{"page_content": ["from page_content"], "content": "from content"}'
        out = _unwrap_envelope(raw)
        assert out in ("from page_content", "from content")
        assert out != "from page_contentfrom content"


class TestUnwrapEnvelopeIntegration:
    """End-to-end: extract_page() pipes the LLM response through _unwrap_envelope."""

    @pytest.mark.asyncio
    async def test_extract_unwraps_json_envelope_response(self) -> None:
        # The exact shape Gemini produced in the smoke loop.
        wrapped = (
            '{"page_content": ['
            '"MEDTRONIC LABS — AI & DATA SCIENCE", '
            '"# AI Coaching Application for Community Health Workers", '
            '"## Use Case Specification & Prioritization Document", '
            '"Purpose: This document presents three core use cases."'
            "]}"
        )
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_make_mock_response(raw_text=wrapped))
        extractor = VisionExtractor(client=client)
        result = await extractor.extract_page(page_image_bytes=b"x", page_label="page_1")
        # The downstream markdown parser looks for `^#` at line starts; the
        # unwrap MUST place the heading markers at column 0.
        assert "# AI Coaching Application for Community Health Workers" in result.markdown
        # And the parser walks line-by-line, so headings must be on their own line.
        for line in result.markdown.split("\n"):
            if line.startswith("# "):
                # The line is just the heading, no JSON syntax around it.
                assert '"' not in line and "[" not in line
