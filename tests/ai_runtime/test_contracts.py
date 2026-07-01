"""W-AI-RUNTIME unit tests — contract additions for v3.3.

Verifies the GenerationType enum carries the new v3.3 task types and that
InferenceRequest accepts optional image_attachments.
"""

import base64

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    _MAX_BASE64_CHARS,
    GenerationConstraints,
    InferenceImage,
    InferenceRequest,
    InferenceResponse,
    ModelPolicy,
    PromptSpec,
    TokenUsage,
    TraceContext,
    TranscribeRequest,
)
from pydantic import ValidationError

# ── GenerationType enum ─────────────────────────────────────────────────


class TestGenerationTypeEnum:
    def test_v33_pipeline_types_present(self) -> None:
        assert GenerationType.OUTLINE_INFERENCE.value == "outline_inference"
        assert GenerationType.MODULE_IDENTIFICATION.value == "module_identification"
        assert GenerationType.CARD_DRAFTING.value == "card_drafting"
        assert GenerationType.MODULE_PUBLISHED_MERGE.value == "module_published_merge"
        assert GenerationType.QUIZ_DRAFTING.value == "quiz_drafting"
        assert GenerationType.DISTRACTOR_CRITIQUE.value == "distractor_critique"
        assert GenerationType.BILINGUAL_TRANSLATION.value == "bilingual_translation"
        assert GenerationType.VISION_EXTRACTION.value == "vision_extraction"
        assert GenerationType.MODULE_SEARCH_METADATA.value == "module_search_metadata"
        assert GenerationType.CARD_SEARCH_METADATA.value == "card_search_metadata"

    def test_enum_round_trip_through_str(self) -> None:
        for gt in GenerationType:
            assert GenerationType(gt.value) is gt

    def test_unknown_value_raises(self) -> None:
        with pytest.raises(ValueError):
            GenerationType("not_a_real_type")


# ── InferenceImage contract ─────────────────────────────────────────────


class TestInferenceImage:
    def test_basic_construction(self) -> None:
        img = InferenceImage(
            mime_type="image/png",
            data_base64=base64.b64encode(b"hello").decode(),
        )
        assert img.mime_type == "image/png"
        assert img.data_base64 == "aGVsbG8="
        assert img.label is None

    def test_optional_label(self) -> None:
        img = InferenceImage(
            mime_type="image/jpeg",
            data_base64=base64.b64encode(b"x").decode(),
            label="page_42",
        )
        assert img.label == "page_42"

    def test_missing_mime_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InferenceImage(data_base64="aGVsbG8=")  # type: ignore[call-arg]

    def test_missing_data_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InferenceImage(mime_type="image/png")  # type: ignore[call-arg]


# ── InferenceRequest with image_attachments ─────────────────────────────


def _make_minimal_request(
    *,
    generation_type: GenerationType = GenerationType.VISION_EXTRACTION,
    image_attachments: list[InferenceImage] | None = None,
) -> InferenceRequest:
    return InferenceRequest(
        request_id="req-1",
        generation_type=generation_type,
        model_policy=ModelPolicy(model="gemini-2.5-flash"),
        prompt=PromptSpec(
            template_id="t-vision-1",
            template_version=1,
            resolved_system_prompt="extract page",
            resolved_human_message="here is the page image",
        ),
        constraints=GenerationConstraints(language="bn", output_format="text"),
        image_attachments=image_attachments or [],
    )


class TestInferenceRequestImages:
    def test_default_empty_attachments(self) -> None:
        req = _make_minimal_request()
        assert req.image_attachments == []

    def test_with_one_image(self) -> None:
        img = InferenceImage(mime_type="image/png", data_base64=base64.b64encode(b"x").decode())
        req = _make_minimal_request(image_attachments=[img])
        assert len(req.image_attachments) == 1
        assert req.image_attachments[0].mime_type == "image/png"

    def test_with_multiple_images(self) -> None:
        imgs = [
            InferenceImage(
                mime_type="image/png",
                data_base64=base64.b64encode(b"a").decode(),
                label="page_1",
            ),
            InferenceImage(
                mime_type="image/png",
                data_base64=base64.b64encode(b"b").decode(),
                label="page_2",
            ),
        ]
        req = _make_minimal_request(image_attachments=imgs)
        assert [i.label for i in req.image_attachments] == ["page_1", "page_2"]

    def test_serialises_to_json_round_trip(self) -> None:
        img = InferenceImage(mime_type="image/png", data_base64=base64.b64encode(b"data").decode())
        req = _make_minimal_request(image_attachments=[img])
        as_json = req.model_dump_json()
        rebuilt = InferenceRequest.model_validate_json(as_json)
        assert rebuilt.image_attachments[0].mime_type == "image/png"
        assert rebuilt.image_attachments[0].data_base64 == "ZGF0YQ=="

    def test_text_only_request_unchanged(self) -> None:
        """Text-only generation types keep working without image attachments."""
        req = _make_minimal_request(generation_type=GenerationType.COACHING_RAG)
        assert req.image_attachments == []
        # Round-trip preserves empty list
        rebuilt = InferenceRequest.model_validate_json(req.model_dump_json())
        assert rebuilt.image_attachments == []


# ── TraceContext v3.3 fields ────────────────────────────────────────────


class TestTraceContextV33:
    def test_pipeline_fields_optional(self) -> None:
        tc = TraceContext()
        assert tc.ingestion_run_id is None
        assert tc.ingestion_run_step_id is None
        assert tc.source_document_id is None
        assert tc.module_candidate_id is None

    def test_pipeline_fields_round_trip(self) -> None:
        tc = TraceContext(
            ingestion_run_id="run-1",
            ingestion_run_step_id="step-3",
            source_document_id="doc-7",
            module_candidate_id="cand-2",
        )
        rebuilt = TraceContext.model_validate_json(tc.model_dump_json())
        assert rebuilt.ingestion_run_id == "run-1"
        assert rebuilt.module_candidate_id == "cand-2"


# ── InferenceResponse parsed_json widened ───────────────────────────────


class TestInferenceResponseParsedJson:
    def test_parsed_json_accepts_dict(self) -> None:
        resp = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.CARD_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            raw_text="{}",
            parsed_json={"cards": [{"id": "c1"}]},
            latency_ms=100,
            token_usage=TokenUsage(input=10, output=20),
        )
        assert isinstance(resp.parsed_json, dict)

    def test_parsed_json_accepts_list(self) -> None:
        """v3.3 widening: module_identification etc. return top-level arrays."""
        resp = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.MODULE_IDENTIFICATION,
            provider="google",
            model="gemini-2.5-pro",
            raw_text="[]",
            parsed_json=[{"title": "candidate 1"}, {"title": "candidate 2"}],
            latency_ms=500,
        )
        assert isinstance(resp.parsed_json, list)
        assert len(resp.parsed_json) == 2

    def test_parsed_json_accepts_none(self) -> None:
        resp = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.COACHING_RAG,
            provider="google",
            model="gemini-2.5-flash",
            raw_text="hi",
            parsed_json=None,
            latency_ms=50,
        )
        assert resp.parsed_json is None


class TestInternalAiPayloadLimits:
    def test_transcribe_request_rejects_oversized_base64(self) -> None:
        with pytest.raises(ValidationError):
            TranscribeRequest(data_base64="A" * (_MAX_BASE64_CHARS + 1), mime_type="audio/mpeg")
