"""W-2 Stage A — vision fallback extraction via ai-runtime.

Per Pipeline v3.3 §4.3. Renders the page to PNG (via page_renderer) and
sends it to ai-runtime through AIRuntimeClient with
generation_type=VISION_EXTRACTION. The verbatim-preserving prompt instructs
the model to return markdown without paraphrasing or translating.

Boundary rule: platform NEVER calls google.generativeai directly. All vision
LLM calls go through ai-runtime (see CLAUDE.md / cursor rules).

Implementation note: Gemini 2.5-flash sometimes wraps markdown output in a
JSON envelope (e.g. `{"page_content": ["# heading", "body para", ...]}`)
even when the prompt asks for plain markdown. We defensively detect the
common envelope shapes and unwrap them so downstream parsers see clean
markdown with `#`/`##`/`###` at line starts.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass

import httpx
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceImage,
    InferenceRequest,
    InferenceResponse,
    TraceContext,
)

from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.llm_text_utils import strip_code_fence
from platform_service.services.prompt_registry import VISION_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.vision_variables import build_vision_variables
from platform_service.workers.extractors.extraction_markdown import normalize_extraction_markdown

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionExtractionResult:
    """Outcome of a vision extraction call for one page."""

    markdown: str
    raw_response: InferenceResponse


class VisionExtractionError(Exception):
    """Raised when vision extraction fails after retries."""


class VisionExtractor:
    """Renders a page to PNG and asks ai-runtime to extract its content."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
    ) -> None:
        self._client = client or get_ai_client()

    async def extract_page(
        self,
        *,
        page_image_bytes: bytes,
        mime_type: str = "image/png",
        page_label: str | None = None,
        trace_context: TraceContext | None = None,
    ) -> VisionExtractionResult:
        """Send a page image to ai-runtime; return the extracted markdown.

        Two distinct outcomes:
        - Provider/transport errors → raise `VisionExtractionError` so the
          caller (Stage 1) can mark the page for the recovery pass and the
          tolerance check.
        - Provider returned successfully but with empty markdown → return
          a `VisionExtractionResult` with `markdown=""`. This is the
          legitimate "blank or decorative page" case. The caller persists
          the empty content; downstream stages treat it as no extractable
          content for that page.

        Treating "empty result" as a failure was the source of the silent-
        chapter-loss bug — pages that genuinely had no text were being
        marked vision_failed and falling back to garbage Bijoy text.
        """
        if not page_image_bytes:
            raise VisionExtractionError("page_image_bytes is empty; nothing to extract")

        rendered = await PromptTemplateService().render(
            None,
            template_id=VISION_TEMPLATE_ID,
            variant_key=None,
            variables=build_vision_variables(),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.VISION_EXTRACTION,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(
                language="bn",  # source-preserving prompt; this hint is informational
                output_format="text",
            ),
            trace_context=trace_context or TraceContext(),
            image_attachments=[
                InferenceImage(
                    mime_type=mime_type,
                    data_base64=base64.b64encode(page_image_bytes).decode(),
                    label=page_label,
                )
            ],
        )

        try:
            response = await self._client.generate(request)
        except (TimeoutError, httpx.HTTPError) as exc:
            # Transport failures (timeout / 5xx / network) for one page must
            # not abort Stage A. Wrap so the orchestrator records this page as
            # vision_failed and keeps processing the remaining pages.
            raise VisionExtractionError(
                f"ai-runtime transport error for page {page_label!r}: {type(exc).__name__}: {exc}"
            ) from exc

        if response.error:
            raise VisionExtractionError(
                f"ai-runtime returned error for page {page_label!r}: {response.error}"
            )
        markdown = _unwrap_envelope(response.raw_text or "")
        # Empty markdown after a successful provider call = legitimate sparse
        # page (cover, divider, decorative). Don't raise; let the caller
        # persist empty content as method='vision'.
        if not markdown:
            logger.info(
                "Vision returned empty markdown for page %r — treating as sparse-page success",
                page_label,
            )
        return VisionExtractionResult(markdown=markdown, raw_response=response)


# Tags Gemini sometimes uses when it wraps the markdown body even though
# we asked for plain text. Tested against actual Gemini 2.5-flash output.
_ENVELOPE_KEYS = ("page_content", "content", "markdown", "text", "body", "page_text")


def _unwrap_envelope(raw: str) -> str:
    """Return clean markdown from Gemini's response.

    Handles three observed shapes:
    1. Plain markdown — returned as-is.
    2. JSON envelope `{"page_content": ["line", "line", ...]}` — extract the
       array (or string) under one of the well-known keys, join with blank
       lines so heading markers land at column 0.
    3. Markdown wrapped in a ```...``` code fence — strip the fence.

    On any parse failure, fall back to the raw string. The downstream
    parser is permissive about extra whitespace.
    """
    s = raw.strip()
    if not s:
        return ""
    # Strip a wrapping ```markdown ... ``` fence first; Gemini sometimes
    # emits one on top of the JSON envelope.
    fenced = strip_code_fence(s)
    if fenced != s:
        s = fenced
    if not s.startswith("{"):
        return normalize_extraction_markdown(s)
    # Try parsing as JSON. If it fails, the LLM probably just returned
    # markdown that happened to start with a brace — treat as raw.
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return normalize_extraction_markdown(s)
    if not isinstance(obj, dict):
        return normalize_extraction_markdown(s)
    for key in _ENVELOPE_KEYS:
        value = obj.get(key)
        if isinstance(value, list):
            # Each list item is one paragraph / heading / list-item line.
            # Join with blank-lines so markdown_outline_parser sees `#`
            # at column 0 of a fresh line.
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return normalize_extraction_markdown("\n\n".join(parts))
        if isinstance(value, str) and value.strip():
            return normalize_extraction_markdown(value.strip())
    # Unknown JSON shape — last-resort: dump the values as text so we at
    # least surface SOME content to the reviewer instead of failing the
    # stage on what was a successful LLM call.
    logger.warning(
        "Vision response was JSON but no known content key matched; keys=%s",
        list(obj.keys()),
    )
    return normalize_extraction_markdown(s)
