"""Internal platform → ai-runtime contract.

Platform sends a fully-resolved InferenceRequest with generation_type (role),
prompt, and content constraints. AI runtime owns model selection and
generation budgets (max_tokens, temperature) via per-GenerationType profiles,
and returns raw output plus applied runtime metadata.
Platform remains responsible for validation, fallback, and telemetry persistence.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from mc_contracts.enums import GenerationType

AiProvider = Literal["google"]

GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES = 20_000_000
# Base64 expands payload ~4/3; cap string length at the inline media limit.
_MAX_BASE64_CHARS = (GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES * 4 + 2) // 3
_MAX_IMAGE_ATTACHMENTS = 20


class PromptSpec(BaseModel):
    template_id: str
    template_version: int
    resolved_system_prompt: str
    resolved_human_message: str
    prompt_template_db_id: UUID | None = None


class GenerationConstraints(BaseModel):
    """Content constraints owned by platform.

    Model id, max_tokens, and temperature are resolved by ai-runtime from
    the request's ``generation_type``.
    """

    language: str = ""
    output_format: str = "json"


class TraceContext(BaseModel):
    session_id: str | None = None
    event_id: str | None = None
    chw_id: int | None = None
    visit_id: str | None = None
    # v3.3 content pipeline references
    ingestion_run_id: str | None = None
    ingestion_run_step_id: str | None = None
    source_document_id: str | None = None
    module_candidate_id: str | None = None


class InferenceImage(BaseModel):
    """Image attachment for vision-capable generation calls.

    Used by GenerationType.VISION_EXTRACTION (Stage A vision fallback) and
    any future multimodal generation types. The image bytes flow as a
    base64-encoded string across the platform → ai-runtime HTTP boundary;
    ai-runtime decodes them to bytes before passing to the provider.
    """

    mime_type: str = Field(..., description="MIME type, e.g. 'image/png' or 'image/jpeg'")
    data_base64: str = Field(..., description="Base64-encoded image bytes", max_length=_MAX_BASE64_CHARS)
    label: str | None = Field(
        None,
        description="Optional label, e.g. 'page_3' — passed through for trace logging only",
    )


class InferenceRequest(BaseModel):
    """Fully resolved request platform sends to ai-runtime."""

    request_id: str
    generation_type: GenerationType
    prompt: PromptSpec
    constraints: GenerationConstraints = Field(default_factory=GenerationConstraints)
    trace_context: TraceContext = Field(default_factory=TraceContext)
    # Extra context payload — varies by generation_type
    context: dict[str, Any] = Field(default_factory=dict)
    # Optional multimodal attachments — used by VISION_EXTRACTION and future
    # vision-capable types. Empty for text-only generation.
    image_attachments: list[InferenceImage] = Field(default_factory=list, max_length=_MAX_IMAGE_ATTACHMENTS)


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0


class InferenceResponse(BaseModel):
    """Raw response from ai-runtime. Platform validates and shapes the final output.

    `parsed_json` widened to dict | list in v3.3 because some new generation
    types return top-level JSON arrays (module_identification candidate list,
    distractor_critique scores, outline_inference sections).
    """

    request_id: str
    generation_type: GenerationType
    provider: AiProvider
    model: str
    max_tokens: int
    temperature: float
    raw_text: str
    parsed_json: dict[str, Any] | list[Any] | None = None
    latency_ms: int
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None


class TranscribeRequest(BaseModel):
    """Media transcription request sent from platform to ai-runtime.

    Media bytes are base64 encoded across the internal HTTP boundary to keep
    the contract JSON-only, matching image attachments on InferenceRequest.
    """

    data_base64: str = Field(
        ..., description="Base64-encoded audio/video bytes", max_length=_MAX_BASE64_CHARS
    )
    mime_type: str = Field(..., description="Media MIME type, e.g. 'audio/mpeg'")


class TranscribeResponse(BaseModel):
    """Speech transcript returned by ai-runtime."""

    text: str


class EmbedRequest(BaseModel):
    """Platform → ai-runtime embed contract."""

    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
