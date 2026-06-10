"""W-5 — translator service.

Glossary-constrained AI translation via ai-runtime
(GenerationType.BILINGUAL_TRANSLATION). Used by the card drafter only when a
field has source coverage in one language but not the other (per the
"Bangla-as-canonical" rule from Pipeline v3.3 §7).

Numerical values are preserved verbatim; clinical glossary is consulted to
keep approved Bangla terminology consistent across the corpus.
"""

from __future__ import annotations

import json
import logging
import uuid

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    ModelPolicy,
    PromptSpec,
    TraceContext,
)

from platform_service.config import get_settings
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient

logger = logging.getLogger(__name__)


_TRANSLATOR_TEMPLATE_ID = "v33-stage-d-translator"
_TRANSLATOR_TEMPLATE_VERSION = 1


def _build_system_prompt(target_language: str) -> str:
    return f"""\
You are translating a single field of clinical training content from one language
to {"Bangla" if target_language == "bn" else "English"}.

GROUND RULES:
- Preserve numerical values verbatim across the translation (e.g. "BP 140/90"
  stays "140/90"; "Hb < 8 g/dL" stays "8 g/dL").
- Use approved clinical terminology from the glossary below — match Bangla
  terms exactly when the glossary specifies them.
- Tone: practical, action-focused, simple vocabulary appropriate for a CHW.
  Do NOT colloquialise; the CHW was trained on the formal training-manual
  vocabulary.
- Output MUST be ONLY the translated text. No commentary, no markdown fences,
  no JSON wrapping.
"""


class TranslatorError(Exception):
    """Raised when the translator can't produce usable output."""


class Translator:
    """Glossary-constrained one-field translator via ai-runtime."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
        *,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or get_ai_client()
        self._model = model or settings.text_model

    async def translate(
        self,
        *,
        source_text: str,
        target_language: str,
        glossary: list[dict[str, str]] | None = None,
        trace_context: TraceContext | None = None,
    ) -> str:
        """Translate `source_text` to `target_language` ('bn' or 'en')."""
        if not source_text or not source_text.strip():
            return ""
        if target_language not in ("bn", "en"):
            raise TranslatorError(f"Unsupported target_language: {target_language!r}")

        glossary = glossary or []
        glossary_payload = {"approved_glossary": glossary, "source_text": source_text}
        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.BILINGUAL_TRANSLATION,
            model_policy=ModelPolicy(model=self._model),
            prompt=PromptSpec(
                template_id=_TRANSLATOR_TEMPLATE_ID,
                template_version=_TRANSLATOR_TEMPLATE_VERSION,
                resolved_system_prompt=_build_system_prompt(target_language),
                resolved_human_message=json.dumps(glossary_payload, ensure_ascii=False, indent=2),
            ),
            constraints=GenerationConstraints(
                language=target_language,
                output_format="text",
            ),
            trace_context=trace_context or TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            raise TranslatorError(f"ai-runtime error: {response.error}")
        out = (response.raw_text or "").strip()
        if not out:
            raise TranslatorError("translator returned empty output")
        return out


def numerical_values_preserved(source: str, translated: str) -> bool:
    """Quick check: every numeric token from source appears verbatim in translation.

    Used as a post-translation guard before persisting AI-translated fields.
    """
    import re

    source_nums = set(re.findall(r"\d+(?:[./,:]\d+)*", source))
    translated_nums = set(re.findall(r"\d+(?:[./,:]\d+)*", translated))
    missing = source_nums - translated_nums
    return not missing
