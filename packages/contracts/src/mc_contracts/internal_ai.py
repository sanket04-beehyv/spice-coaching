"""Internal platform → ai-runtime contract.

Platform sends a fully-resolved InferenceRequest.
AI runtime returns raw output + runtime metadata.
Platform remains responsible for validation, fallback, and telemetry persistence.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from mc_contracts.enums import GenerationType

AiProvider = Literal["google", "openai"]

OPENAI_TRANSCRIPTION_MAX_BYTES = 25_000_000
GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES = 20_000_000
# Base64 expands payload ~4/3; cap string length at the largest inline media limit.
_MAX_BASE64_CHARS = (max(OPENAI_TRANSCRIPTION_MAX_BYTES, GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES) * 4 + 2) // 3
_MAX_IMAGE_ATTACHMENTS = 20


class ModelPolicy(BaseModel):
    """Model id for the inference call.

    Provider routing (google vs openai) is owned by ai-runtime via ``AI_CLOUD_PROVIDER``
    / ``ai_provider``. Platform selects ``model`` names from its own ``ai_cloud_provider``
    setting, which must match the ai-runtime deployment.
    """

    model: str = "gemini-2.5-flash"


class PromptSpec(BaseModel):
    template_id: str
    template_version: int
    resolved_system_prompt: str
    resolved_human_message: str


class GenerationConstraints(BaseModel):
    language: str = "bn"
    output_format: str = "json"
    max_tokens: int | None = None
    temperature: float | None = None


class TraceContext(BaseModel):
    scenario_id: str | None = None
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
    model_policy: ModelPolicy
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


# ── Typed context payloads per generation_type ───────────────────────────────


class CounsellingContext(BaseModel):
    """context payload for GenerationType.COUNSELLING."""

    patient_snapshot: dict[str, Any]
    chw_context: dict[str, Any]
    matched_sop_excerpt: str
    matched_key_points: list[str]
    matched_danger_signs: list[Any]
    matched_referral_threshold: Any
    bangla_card_excerpt: str
    spice_referral_options: list[str]
    clinical_domain: str | None
    action_type: str | None


class ITHelpContext(BaseModel):
    """context payload for GenerationType.IT_HELP."""

    question: str
    chw_id: int | None = None


class ExtractionContext(BaseModel):
    """context payload for GenerationType.EXTRACTION — SOP section → scenario fields."""

    section_text: str
    source_doc: str
    source_page: int | None = None


class QuizContext(BaseModel):
    """context payload for GenerationType.QUIZ."""

    scenario_id: str
    sop_excerpt_en: str
    key_points: list[str]
    clinical_domain: str
    max_questions: int = 3
