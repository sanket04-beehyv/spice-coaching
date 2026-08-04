"""Unit tests for ai-runtime GenerationType → GenerationProfile map."""

from __future__ import annotations

from ai_runtime.generation_profiles import GENERATION_PROFILES
from mc_contracts.enums import GenerationType


def test_every_generation_type_has_a_profile() -> None:
    for generation_type in GenerationType:
        assert generation_type in GENERATION_PROFILES, f"{generation_type!r} missing from GENERATION_PROFILES"


def test_key_profile_budgets() -> None:
    assert GENERATION_PROFILES[GenerationType.MODULE_IDENTIFICATION].max_tokens == 12_000
    assert GENERATION_PROFILES[GenerationType.MODULE_PUBLISHED_MERGE].max_tokens == 4_000
    assert GENERATION_PROFILES[GenerationType.COACHING_RAG].max_tokens == 2048
    assert GENERATION_PROFILES[GenerationType.RAG_EVAL_JUDGE].temperature == 0.0
