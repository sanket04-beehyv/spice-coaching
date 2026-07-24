"""Per-GenerationType inference profiles owned by ai-runtime.

Platform sends only ``generation_type`` (role). This map resolves the concrete
model id and workflow budgets (max_tokens, temperature) for each call.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_contracts.enums import GenerationType

from ai_runtime.config import Settings


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    model: str
    max_tokens: int
    temperature: float


_DEFAULT_MODEL = "gemini-2.5-flash"
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_TEMPERATURE = 0.2


def _profile(
    *,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> GenerationProfile:
    return GenerationProfile(model=model, max_tokens=max_tokens, temperature=temperature)


# Complete map — every GenerationType must appear here.
GENERATION_PROFILES: dict[GenerationType, GenerationProfile] = {
    GenerationType.OUTLINE_INFERENCE: _profile(),
    GenerationType.MODULE_IDENTIFICATION: _profile(max_tokens=12_000),
    GenerationType.CROSS_SOURCE_FUSION: _profile(max_tokens=8192),
    GenerationType.CARD_DRAFTING: _profile(),
    GenerationType.MODULE_PUBLISHED_MERGE: _profile(max_tokens=4_000),
    GenerationType.QUIZ_DRAFTING: _profile(),
    GenerationType.DISTRACTOR_CRITIQUE: _profile(),
    GenerationType.BILINGUAL_TRANSLATION: _profile(),
    GenerationType.VISION_EXTRACTION: _profile(),
    GenerationType.COACHING_RAG: _profile(max_tokens=2048),
    GenerationType.RAG_EVAL_JUDGE: _profile(max_tokens=256, temperature=0.0),
    GenerationType.MODULE_GAP_CLASSIFICATION: _profile(),
    GenerationType.MODULE_ASSESSMENT_TOPIC_CLASSIFICATION: _profile(),
    GenerationType.MODULE_SEARCH_METADATA: _profile(),
    GenerationType.CARD_SEARCH_METADATA: _profile(),
    GenerationType.CHAT_FAQ_SYNTHESIS: _profile(),
    GenerationType.MODULE_DEMAND_SUMMARY: _profile(),
    GenerationType.CHAT_FEEDBACK_SUMMARY: _profile(),
}


def resolve_profile(generation_type: GenerationType, settings: Settings) -> GenerationProfile:
    """Return the profile for ``generation_type``.

    Falls back to Settings defaults only if the map is missing an entry
    (should not happen — tests assert completeness).
    """
    profile = GENERATION_PROFILES.get(generation_type)
    if profile is not None:
        return profile
    return GenerationProfile(
        model=settings.default_inference_model,
        max_tokens=settings.default_max_tokens,
        temperature=settings.default_temperature,
    )
