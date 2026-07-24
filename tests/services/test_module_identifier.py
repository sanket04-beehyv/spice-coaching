"""Layer 2 — Stage 2 module-identification tests.

Architecture-reset regressions covered:

- The system prompt does NOT mention any behavioural-gap registry, gap_code,
  or "fit candidates to gaps" instructions.
- The candidate JSON contract dropped `behavioural_gap_code` from required
  fields; validator must accept candidates without it.
- The `identify()` signature has NO `behavioural_gap_registry` or
  `valid_gap_codes` kwargs anymore.
- Template version was bumped (cache invalidation for the gap-context-free
  prompt).

Also covers:
- LLM output shape tolerance (top-level array vs `{"candidates": [...]}` wrapper).
- Per-candidate validator (card/quiz bounds, module_type whitelist, block-id
  membership, malformed provenance).
- Authority-kind branch picking in the system prompt.
- Error paths: provider error, malformed JSON, unknown payload shape.

All pure-unit, no DB, no network.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
)
from platform_service.services.ingestion_cardinality import IngestionCardinality
from platform_service.services.module_identifier import (
    ModuleIdentifier,
    ModuleIdentifierError,
    _apply_ingestion_instruction_gate,
    _extract_candidates,
    _validate_candidate,
)
from platform_service.services.prompt_id_codec import PromptIdCodec
from platform_service.services.prompts.module_identifier_prompt import (
    render_human_message,
    render_system_prompt,
)

pytestmark = pytest.mark.usefixtures("mock_prompt_templates")

# ─── Fixtures ──────────────────────────────────────────────────────────────


def _valid_candidate(
    *,
    title: str = "Sample Module",
    cards: int = 5,
    quiz: int = 4,
    module_type: str = "refresher",
    cited_block_ids: list[UUID] | None = None,
    source_page_id: UUID | None = None,
) -> dict[str, Any]:
    block_ids = cited_block_ids or [uuid4()]
    page_id = source_page_id or uuid4()
    return {
        "proposed_title": title,
        "scope_summary": "A summary that explains the topic.",
        "source_provenance": [
            {
                "source_document_id": str(uuid4()),
                "source_page_id": str(page_id),
                "content_block_ids": [str(b) for b in block_ids],
            }
        ],
        "estimated_card_count": cards,
        "estimated_quiz_count": quiz,
        "proposed_module_type": module_type,
        "clinical_review_notes": "Validate clinical thresholds.",
    }


def _mock_response(
    parsed_json: Any = None, *, raw_text: str = "", error: str | None = None
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.MODULE_IDENTIFICATION,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=12_000,
        temperature=0.2,
        raw_text=raw_text,
        parsed_json=parsed_json,
        latency_ms=200,
        token_usage=TokenUsage(input=100, output=200),
        error=error,
    )


# ─── Prompt: gap context removed (architecture-reset regression) ───────────


class TestPromptGapContextRemoved:
    @pytest.mark.parametrize(
        "content_domains",
        [
            set(),
            {"clinical"},
            {"digital"},
        ],
    )
    def test_system_prompt_does_not_reference_gap_registry(self, content_domains: set[str]) -> None:
        prompt = render_system_prompt(content_domains)
        lowered = prompt.lower()
        assert "behavioural_gap" not in lowered
        assert "behavioural gap" not in lowered
        assert "gap_code" not in lowered
        assert "gap registry" not in lowered

    def test_system_prompt_does_not_request_gap_code_field(self) -> None:
        prompt = render_system_prompt({"clinical"})
        assert "behavioural_gap_code" not in prompt

    def test_human_message_omits_behavioural_gap_registry_payload(self) -> None:
        msg = render_human_message(
            already_published_modules=[],
            document_outlines=[
                {
                    "source_document_id": str(uuid4()),
                    "outline_method": "markdown_parser",
                    "sections": [{"heading": "Chapter 1"}],
                }
            ],
            page_corpus=[],
            codec=PromptIdCodec.from_corpus([]),
        )
        assert "behavioural_gap_registry" not in msg
        assert "behavioural_gap_registry_repeat" not in msg

    def test_human_message_includes_already_published_modules(self) -> None:
        msg = render_human_message(
            already_published_modules=[{"module_code": "anc-referral", "title": {"bn": "ANC Referral"}}],
            document_outlines=[],
            page_corpus=[],
            codec=PromptIdCodec.from_corpus([]),
        )
        assert "already_published_modules" in msg
        assert "anc-referral" in msg

    def test_template_version_at_least_5(self) -> None:
        """v5 added the annexure-exclusion rule (Hindi NCDs run was
        proposing modules from Annexure 1-4). Tests must fail if someone
        reverts to v4 (no annexure rule) or earlier."""
        prompt = render_system_prompt({"clinical"})
        assert "annexure" in prompt.lower() or "appendix" in prompt.lower()

    def test_system_prompt_requests_domain_field_with_common_examples(self) -> None:
        prompt = render_system_prompt({"clinical"})
        assert '"domain":' in prompt
        assert "prefer" in prompt.lower()
        assert "anc" in prompt
        assert "hypertension" in prompt
        assert "must be one of" not in prompt.lower()

    def test_system_prompt_includes_ingestion_guidance_when_instructions_set(self) -> None:
        prompt = render_system_prompt(
            {"clinical"},
            ingestion_instructions="Focus on referral workflows.",
        )
        assert "INGESTION GUIDANCE MODE" in prompt
        assert "USER_INGESTION_GUIDANCE" in prompt
        assert "ingestion_instruction_rationale" in prompt
        assert "HARD scope filter" in prompt

    def test_system_prompt_omits_ingestion_guidance_when_instructions_absent(self) -> None:
        prompt = render_system_prompt({"clinical"})
        assert "INGESTION GUIDANCE MODE" not in prompt
        assert "ingestion_instruction_rationale" not in prompt

    def test_human_message_includes_guidance_when_instructions_set(self) -> None:
        msg = render_human_message(
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            codec=PromptIdCodec.from_corpus([]),
            ingestion_instructions="Focus on referral workflows.",
        )
        assert "## USER_INGESTION_GUIDANCE ##" in msg
        assert "<<<BEGIN_ADMIN_STEERING>>>" in msg
        assert "Focus on referral workflows." in msg

    def test_human_message_omits_guidance_when_instructions_absent(self) -> None:
        msg = render_human_message(
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            codec=PromptIdCodec.from_corpus([]),
        )
        assert "USER_INGESTION_GUIDANCE" not in msg
        assert "BEGIN_ADMIN_STEERING" not in msg

    def test_human_message_single_page_citation_note(self) -> None:
        page_id = uuid4()
        block_id = uuid4()
        corpus = [
            {
                "source_document_id": str(uuid4()),
                "content_domain": "clinical",
                "primary_language": "en",
                "pages": [
                    {
                        "source_page_id": str(page_id),
                        "page_number": 1,
                        "blocks": [
                            {
                                "content_block_id": str(block_id),
                                "block_type": "paragraph",
                                "content_text": "Sample text",
                            }
                        ],
                    }
                ],
            }
        ]
        msg = render_human_message(
            already_published_modules=[],
            document_outlines=[],
            page_corpus=corpus,
            codec=PromptIdCodec.from_corpus(corpus),
        )
        assert "exactly ONE page" in msg
        assert "only valid source_page_id token is p1" in msg

    def test_system_prompt_excludes_annexures(self) -> None:
        """The grouping rules must explicitly forbid proposing modules
        from forms / checklists / reference tables. Detection cues for
        annexure terms must be in the prompt body so the LLM has something
        to match against.
        """
        prompt = render_system_prompt({"clinical"}, deployment_primary_locale="en")
        lowered = prompt.lower()
        assert "annexure" in lowered
        assert "appendix" in lowered
        assert "job aid" in lowered

    def test_human_message_uses_short_tokens_not_uuids(self) -> None:
        """The corpus body must reference content blocks/pages/docs by
        short tokens (d1/p47/b231), NOT by raw UUIDs. Regressing to
        raw UUIDs re-introduces the transcription failure mode."""
        doc_id = str(uuid4())
        page_id = str(uuid4())
        block_id = str(uuid4())
        page_corpus = [
            {
                "source_document_id": doc_id,
                "content_domain": "clinical",
                "primary_language": "bn",
                "pages": [
                    {
                        "source_page_id": page_id,
                        "page_number": 1,
                        "blocks": [
                            {
                                "content_block_id": block_id,
                                "block_type": "paragraph",
                                "content_text": "hello",
                            }
                        ],
                    }
                ],
            }
        ]
        codec = PromptIdCodec.from_corpus(page_corpus)
        msg = render_human_message(
            already_published_modules=[],
            document_outlines=[],
            page_corpus=page_corpus,
            codec=codec,
        )
        # The body's CORPUS section must show short tokens, not raw UUIDs.
        assert "source_document_id=d1" in msg
        assert "source_page_id=p1" in msg
        assert "content_block_id=b1" in msg
        # Raw UUIDs must not appear inside the corpus body. (They may
        # still appear in the head/tail outline JSON, which is fine —
        # the LLM doesn't cite outline UUIDs.)
        corpus_section = msg.split("## CORPUS ##", 1)[1].split("\n\n", 1)[0]
        assert doc_id not in corpus_section
        assert page_id not in corpus_section
        assert block_id not in corpus_section


# ─── Authority-kind branch selection ───────────────────────────────────────


class TestContentDomainBranching:
    def test_clinical_branch_is_default(self) -> None:
        prompt = render_system_prompt(set())
        assert "CLINICAL" in prompt or "initial_training" in prompt

    def test_digital_branch(self) -> None:
        prompt = render_system_prompt({"digital"})
        assert "DIGITAL" in prompt
        assert "digital_proficiency" in prompt

    def test_clinical_with_app_workflows_branch(self) -> None:
        prompt = render_system_prompt({"clinical_with_app_workflows"})
        assert "CLINICAL" in prompt and "app" in prompt.lower()
        assert "initial_training" in prompt


# ─── _extract_candidates: tolerant to LLM output shape ─────────────────────


class TestExtractCandidates:
    def test_top_level_array_extracted(self) -> None:
        payload = [{"a": 1}, {"b": 2}]
        assert _extract_candidates(payload) == [{"a": 1}, {"b": 2}]

    def test_dict_with_candidates_key_extracted(self) -> None:
        payload = {"candidates": [{"a": 1}, {"b": 2}]}
        assert _extract_candidates(payload) == [{"a": 1}, {"b": 2}]

    def test_non_dict_entries_filtered_out(self) -> None:
        payload = [{"a": 1}, "not a dict", 42, {"b": 2}]
        assert _extract_candidates(payload) == [{"a": 1}, {"b": 2}]

    def test_unknown_shape_raises(self) -> None:
        with pytest.raises(ModuleIdentifierError, match="not recognised"):
            _extract_candidates("just a string")
        with pytest.raises(ModuleIdentifierError, match="not recognised"):
            _extract_candidates(42)

    def test_dict_without_candidates_key_raises(self) -> None:
        with pytest.raises(ModuleIdentifierError, match="not recognised"):
            _extract_candidates({"weird_root": [{"a": 1}]})

    def test_empty_array_accepted(self) -> None:
        assert _extract_candidates([]) == []
        assert _extract_candidates({"candidates": []}) == []


# ─── _validate_candidate: per-candidate gates ──────────────────────────────


class TestValidateCandidate:
    def test_valid_candidate_passes(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        assert _validate_candidate(cand, valid_block_ids={b1}) is True

    def test_catalogued_domain_normalized_on_candidate(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["domain"] = "Hypertension"
        assert _validate_candidate(cand, valid_block_ids={b1}) is True
        assert cand["domain"] == "hypertension"

    def test_unknown_domain_normalized_and_kept(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["domain"] = "Dengue Fever"
        assert _validate_candidate(cand, valid_block_ids={b1}) is True
        assert cand["domain"] == "dengue_fever"

    def test_no_behavioural_gap_code_required(self) -> None:
        """Architecture-reset: gap_code dropped from required fields."""
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        assert "behavioural_gap_code" not in cand
        assert _validate_candidate(cand, valid_block_ids={b1}) is True

    def test_missing_proposed_title_rejected(self) -> None:
        cand = _valid_candidate()
        del cand["proposed_title"]
        assert _validate_candidate(cand, valid_block_ids=set()) is False

    def test_missing_scope_summary_rejected(self) -> None:
        cand = _valid_candidate()
        del cand["scope_summary"]
        assert _validate_candidate(cand, valid_block_ids=set()) is False

    def test_missing_source_provenance_rejected(self) -> None:
        cand = _valid_candidate()
        del cand["source_provenance"]
        assert _validate_candidate(cand, valid_block_ids=set()) is False

    def test_invalid_module_type_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(module_type="totally_made_up", cited_block_ids=[b1])
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    @pytest.mark.parametrize(
        "module_type",
        ["refresher", "content_update", "digital_proficiency", "initial_training"],
    )
    def test_known_module_types_accepted(self, module_type: str) -> None:
        b1 = uuid4()
        cand = _valid_candidate(module_type=module_type, cited_block_ids=[b1])
        assert _validate_candidate(cand, valid_block_ids={b1}) is True

    @pytest.mark.parametrize(
        "card_count,expected",
        [(2, False), (3, True), (5, True), (7, True), (8, True), (10, True), (11, False)],
    )
    def test_card_count_bounds(self, card_count: int, expected: bool) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cards=card_count, cited_block_ids=[b1])
        assert _validate_candidate(cand, valid_block_ids={b1}) is expected

    @pytest.mark.parametrize(
        "quiz_count,expected",
        [(2, False), (3, True), (10, True), (11, False)],
    )
    def test_quiz_count_bounds(self, quiz_count: int, expected: bool) -> None:
        b1 = uuid4()
        cand = _valid_candidate(quiz=quiz_count, cited_block_ids=[b1])
        assert _validate_candidate(cand, valid_block_ids={b1}) is expected

    def test_non_integer_counts_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["estimated_card_count"] = "five"
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    def test_empty_provenance_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"] = []
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    def test_block_id_not_in_corpus_rejected(self) -> None:
        b1, b_fake = uuid4(), uuid4()
        cand = _valid_candidate(cited_block_ids=[b_fake])
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    def test_invalid_uuid_in_provenance_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"][0]["content_block_ids"] = ["not-a-uuid"]
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    def test_provenance_entry_not_dict_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"] = ["not a dict"]
        assert _validate_candidate(cand, valid_block_ids={b1}) is False

    def test_content_block_ids_not_a_list_rejected(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"][0]["content_block_ids"] = "not a list"
        assert _validate_candidate(cand, valid_block_ids={b1}) is False


# ─── Ingestion instruction rationale gate ──────────────────────────────────


class TestIngestionInstructionGate:
    def test_keeps_candidate_with_rationale_when_instructions_present(self) -> None:
        cand = _valid_candidate()
        cand["ingestion_instruction_rationale"] = "Guidance asked for ANC referral."
        kept = _apply_ingestion_instruction_gate(
            [cand],
            ingestion_instructions="Focus on ANC referral.",
        )
        assert kept == [cand]
        assert cand["ingestion_instruction_rationale"] == "Guidance asked for ANC referral."

    def test_drops_candidate_without_rationale_when_instructions_present(self) -> None:
        cand = _valid_candidate()
        kept = _apply_ingestion_instruction_gate(
            [cand],
            ingestion_instructions="Focus on ANC referral.",
        )
        assert kept == []

    def test_accepts_missing_rationale_when_instructions_absent(self) -> None:
        cand = _valid_candidate()
        kept = _apply_ingestion_instruction_gate([cand], ingestion_instructions=None)
        assert kept == [cand]
        assert cand.get("ingestion_instruction_rationale") is None

    def test_strips_whitespace_rationale(self) -> None:
        cand = _valid_candidate()
        cand["ingestion_instruction_rationale"] = "  maps to guidance  "
        kept = _apply_ingestion_instruction_gate(
            [cand],
            ingestion_instructions="Focus on referral.",
        )
        assert len(kept) == 1
        assert kept[0]["ingestion_instruction_rationale"] == "maps to guidance"

    def test_empty_rationale_treated_as_missing_when_instructions_present(self) -> None:
        cand = _valid_candidate()
        cand["ingestion_instruction_rationale"] = "   "
        kept = _apply_ingestion_instruction_gate(
            [cand],
            ingestion_instructions="Focus on referral.",
        )
        assert kept == []
        assert cand["ingestion_instruction_rationale"] is None


# ─── ModuleIdentifier.identify: end-to-end with mocked client ──────────────


class TestIdentifyHappyPath:
    @pytest.mark.asyncio
    async def test_returns_validated_candidates_dict_payload(self) -> None:
        b1, p1 = uuid4(), uuid4()
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {"candidates": [_valid_candidate(cited_block_ids=[b1], source_page_id=p1)]}
            )
        )
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains={"clinical"},
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
        )
        assert len(result.candidates) == 1
        assert result.candidates[0]["proposed_title"] == "Sample Module"

    @pytest.mark.asyncio
    async def test_top_level_array_payload_accepted(self) -> None:
        b1, p1 = uuid4(), uuid4()
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response([_valid_candidate(cited_block_ids=[b1], source_page_id=p1)])
        )
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains={"clinical"},
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
        )
        assert len(result.candidates) == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_text_json_when_parsed_json_none(self) -> None:
        b1, p1 = uuid4(), uuid4()
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                None,
                raw_text=json.dumps(
                    {"candidates": [_valid_candidate(cited_block_ids=[b1], source_page_id=p1)]}
                ),
            )
        )
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains={"clinical"},
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
        )
        assert len(result.candidates) == 1


class TestIdentifyCallShape:
    @pytest.mark.asyncio
    async def test_uses_module_identification_generation_type(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response({"candidates": []}))
        identifier = ModuleIdentifier(client=client)
        await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids=set(),
            valid_page_ids=set(),
        )
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert sent.generation_type == GenerationType.MODULE_IDENTIFICATION
        assert sent.constraints.output_format == "json"

    @pytest.mark.asyncio
    async def test_request_does_not_send_model_policy(self) -> None:
        """Model selection is owned by ai-runtime generation profiles."""
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response({"candidates": []}))
        identifier = ModuleIdentifier(client=client)
        await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids=set(),
            valid_page_ids=set(),
        )
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert "model_policy" not in sent.model_dump()
        assert sent.generation_type == GenerationType.MODULE_IDENTIFICATION


class TestIdentifyValidationFiltersInvalid:
    @pytest.mark.asyncio
    async def test_invalid_candidate_silently_dropped(self) -> None:
        b1, p1 = uuid4(), uuid4()
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "candidates": [
                        _valid_candidate(cited_block_ids=[b1], source_page_id=p1),  # valid
                        _valid_candidate(
                            cards=1, cited_block_ids=[b1], source_page_id=p1
                        ),  # bad: card_count below min
                        _valid_candidate(
                            module_type="bogus", cited_block_ids=[b1], source_page_id=p1
                        ),  # bad: module_type
                    ]
                }
            )
        )
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
        )
        # Only the valid one survives.
        assert len(result.candidates) == 1
        assert result.candidates[0]["estimated_card_count"] == 5

    @pytest.mark.asyncio
    async def test_drops_candidates_without_rationale_when_instructions_present(self) -> None:
        b1, p1 = uuid4(), uuid4()
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "candidates": [
                        _valid_candidate(cited_block_ids=[b1], source_page_id=p1),
                    ]
                }
            )
        )
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
            ingestion_instructions="Focus on ANC referral.",
        )
        assert result.candidates == []

    @pytest.mark.asyncio
    async def test_keeps_candidates_with_rationale_when_instructions_present(self) -> None:
        b1, p1 = uuid4(), uuid4()
        cand = _valid_candidate(cited_block_ids=[b1], source_page_id=p1)
        cand["ingestion_instruction_rationale"] = "Corpus chapter 3 covers ANC referral."
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response({"candidates": [cand]}))
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1},
            valid_page_ids={p1},
            ingestion_instructions="Focus on ANC referral.",
        )
        assert len(result.candidates) == 1
        assert (
            result.candidates[0]["ingestion_instruction_rationale"] == "Corpus chapter 3 covers ANC referral."
        )


class TestIdentifyErrorPaths:
    @pytest.mark.asyncio
    async def test_runtime_error_raises_module_identifier_error(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, error="Vertex 429 rate-limited"))
        identifier = ModuleIdentifier(client=client)
        with pytest.raises(ModuleIdentifierError, match="ai-runtime error"):
            await identifier.identify(
                content_domains=set(),
                already_published_modules=[],
                document_outlines=[],
                page_corpus=[],
                valid_block_ids=set(),
                valid_page_ids=set(),
            )

    @pytest.mark.asyncio
    async def test_invalid_json_raw_text_raises(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text="not json {"))
        identifier = ModuleIdentifier(client=client)
        # The salvage path runs first; with no completable elements it
        # surfaces the truncation message rather than the raw JSONDecode
        # error. This is the friendlier signal — the dashboard knows to
        # bump the token budget or partition more aggressively.
        with pytest.raises(ModuleIdentifierError, match="truncated"):
            await identifier.identify(
                content_domains=set(),
                already_published_modules=[],
                document_outlines=[],
                page_corpus=[],
                valid_block_ids=set(),
                valid_page_ids=set(),
            )

    @pytest.mark.asyncio
    async def test_unknown_payload_shape_raises(self) -> None:
        # raw_text parses to JSON but is a bare integer — neither dict nor array.
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text="42"))
        identifier = ModuleIdentifier(client=client)
        with pytest.raises(ModuleIdentifierError, match="not recognised"):
            await identifier.identify(
                content_domains=set(),
                already_published_modules=[],
                document_outlines=[],
                page_corpus=[],
                valid_block_ids=set(),
                valid_page_ids=set(),
            )


# ─── F1 review fixes: truncation salvage + UUID-pair sanity ────────────────


class TestF1TruncationSalvage:
    """The SK-PDF smoke run hit max_output_tokens mid-string; the parser
    must surface partial recovery instead of failing the whole stage. This
    is the only path that lets a long real-world manual produce ANY modules
    when Gemini gets greedy with thinking tokens."""

    @pytest.mark.asyncio
    async def test_truncated_array_recovers_complete_candidates(self) -> None:
        """Two complete candidates, third candidate cut off mid-string —
        parsed_json is None (ai-runtime salvage didn't run / returned None),
        and the local salvage in module_identifier kicks in."""
        b1, b2 = uuid4(), uuid4()
        page1 = uuid4()
        # Build a real Stage 2 array that's then truncated mid-third-element.
        good_a = _valid_candidate(title="A", cited_block_ids=[b1])
        good_b = _valid_candidate(title="B", cited_block_ids=[b2])
        good_a["source_provenance"][0]["source_page_id"] = str(page1)
        good_b["source_provenance"][0]["source_page_id"] = str(page1)
        truncated = (
            "["
            + json.dumps(good_a, ensure_ascii=False)
            + ","
            + json.dumps(good_b, ensure_ascii=False)
            + ',{"proposed_title": "C", "scope_summary": "cut'  # ← truncation
        )

        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text=truncated))
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1, b2},
            valid_page_ids={page1},
        )
        # Two complete candidates recovered.
        assert result.truncated is True
        titles = {c["proposed_title"] for c in result.candidates}
        assert titles == {"A", "B"}

    @pytest.mark.asyncio
    async def test_truncated_wrapped_object_recovers_via_inner_array_salvage(
        self,
    ) -> None:
        """P1 regression: the identifier prompt instructs the LLM to emit
        `{"candidates": [...]}` (object-wrapped). Without wrapper-aware
        salvage, the original SK-PDF failure mode reappears whenever a
        single-call run truncates — `_salvage_truncated_array` returned
        None for non-`[`-prefixed text and the whole stage failed even
        when N complete candidates were recoverable inside the wrapper."""
        b1, b2 = uuid4(), uuid4()
        page1 = uuid4()
        good_a = _valid_candidate(title="A", cited_block_ids=[b1], source_page_id=page1)
        good_b = _valid_candidate(title="B", cited_block_ids=[b2], source_page_id=page1)
        truncated = (
            '{"candidates": ['
            + json.dumps(good_a, ensure_ascii=False)
            + ","
            + json.dumps(good_b, ensure_ascii=False)
            + ',{"proposed_title": "C", "scope_summary": "cut'  # ← truncation
        )
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text=truncated))
        identifier = ModuleIdentifier(client=client)
        result = await identifier.identify(
            content_domains=set(),
            already_published_modules=[],
            document_outlines=[],
            page_corpus=[],
            valid_block_ids={b1, b2},
            valid_page_ids={page1},
        )
        assert result.truncated is True
        titles = {c["proposed_title"] for c in result.candidates}
        assert titles == {"A", "B"}

    @pytest.mark.asyncio
    async def test_truncated_with_zero_complete_candidates_raises_friendly_error(
        self,
    ) -> None:
        """When even the first element is incomplete, surface a message
        that points the operator at the levers (max_output_tokens /
        max_corpus_tokens)."""
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text='[{"proposed_title": "Cut'))
        identifier = ModuleIdentifier(client=client)
        with pytest.raises(ModuleIdentifierError, match="truncated"):
            await identifier.identify(
                content_domains=set(),
                already_published_modules=[],
                document_outlines=[],
                page_corpus=[],
                valid_block_ids=set(),
                valid_page_ids=set(),
            )


class TestF1UuidPairSanity:
    """The SK-PDF smoke run had Gemini copy-paste a single UUID across
    `source_page_id` AND `content_block_ids` for many candidates — a
    physical impossibility (different tables). Reject those candidates."""

    def test_rejects_when_source_page_id_collides_with_content_block_id(self) -> None:
        bad_uuid = uuid4()  # appears as both page and block (impossible)
        cand = _valid_candidate(cited_block_ids=[bad_uuid])
        cand["source_provenance"][0]["source_page_id"] = str(bad_uuid)
        assert _validate_candidate(cand, valid_block_ids={bad_uuid}) is False

    def test_rejects_when_source_page_id_not_in_corpus(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"][0]["source_page_id"] = str(uuid4())  # not in corpus
        # With valid_page_ids supplied, the unknown page is rejected.
        assert _validate_candidate(cand, valid_block_ids={b1}, valid_page_ids={uuid4()}) is False

    def test_passes_when_source_page_id_is_valid_and_distinct(self) -> None:
        b1 = uuid4()
        page = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        cand["source_provenance"][0]["source_page_id"] = str(page)
        assert _validate_candidate(cand, valid_block_ids={b1}, valid_page_ids={page}) is True

    def test_valid_page_ids_omitted_skips_membership_check(self) -> None:
        """Backwards-compat: if the caller doesn't supply valid_page_ids
        (older callers), only the collision check applies — the
        membership check is skipped."""
        b1 = uuid4()
        cand = _valid_candidate(cited_block_ids=[b1])
        # source_page_id is NOT in any corpus, but no valid_page_ids supplied.
        assert _validate_candidate(cand, valid_block_ids={b1}) is True

    def test_fixed_cardinality_target_rejects_wrong_card_count(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cards=4, cited_block_ids=[b1])
        cardinality = IngestionCardinality(target_cards=5, target_quizzes=None)
        assert _validate_candidate(cand, valid_block_ids={b1}, cardinality=cardinality) is False

    def test_fixed_cardinality_target_accepts_exact_card_count(self) -> None:
        b1 = uuid4()
        cand = _valid_candidate(cards=5, cited_block_ids=[b1])
        cardinality = IngestionCardinality(target_cards=5, target_quizzes=4)
        assert _validate_candidate(cand, valid_block_ids={b1}, cardinality=cardinality) is True
