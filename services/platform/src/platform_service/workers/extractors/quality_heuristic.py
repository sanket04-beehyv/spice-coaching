"""W-2 Stage A — text-extraction quality heuristic.

Per Pipeline v3.3 §4.2. Pure-function deterministic scoring on text-extracted
output. No LLM calls. Decides whether vision fallback is needed for a page.

The heuristic produces a single binary pass/fail with no ambiguous middle
zone (the v3.3 fix for Pipeline §4.2's previous 20-70% gap):
- text_empty: < min_chars extracted → fail
- encoding_integrity (when document language has a known native script):
  native-script codepoint density < min AND non-ASCII byte rate > max → fail
  (signal of legacy ANSI/Bijoy fonts producing mangled bytes — generalised
  from Bangla-only to Bangla/Hindi/Tamil/Telugu after the ASHA-NCDs-Hindi
  ingest, where Bijoy mojibake bypassed the bn-only check, routed pages
  to text path, and the identifier silently dropped TB & FP modules
  because their canonical names appeared only as mojibake'd ASCII).
- heading_absence (multi-page only): zero detected headings → fail
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from platform_service.config import get_settings

# Native-script Unicode ranges by primary_language code. Add new languages
# here as they come up in real documents (the SourceDocument.primary_language
# is a free-form string today; we just match what we know).
_NATIVE_SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "bn": (0x0980, 0x09FF),  # Bengali
    "bn_en_mixed": (0x0980, 0x09FF),  # Bengali (mixed bn/en docs)
    "hi": (0x0900, 0x097F),  # Devanagari
    "mr": (0x0900, 0x097F),  # Marathi (also Devanagari)
    "ta": (0x0B80, 0x0BFF),  # Tamil
    "te": (0x0C00, 0x0C7F),  # Telugu
}
# Markdown heading prefix: ^#{1,6}\s
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


@dataclass(frozen=True)
class QualityScore:
    """Per-page text-extraction quality result.

    `passed` is the binary "use text vs fallback to vision" decision.
    The other fields are recorded on the SourcePage row for triage and
    threshold tuning later.
    """

    passed: bool
    composite_score: float
    text_char_count: int
    native_script_ratio: float  # codepoint density in the doc's expected native script
    non_ascii_byte_ratio: float
    heading_count: int
    fail_reasons: tuple[str, ...]


def _native_script_ratio(text: str, primary_language: str) -> float:
    """Codepoint density in the doc's declared native script. 0.0 if the
    language has no registered native script (e.g. 'en')."""
    rng = _NATIVE_SCRIPT_RANGES.get(primary_language)
    if rng is None or not text:
        return 0.0
    native_chars = sum(1 for ch in text if rng[0] <= ord(ch) <= rng[1])
    # Count alphabetic/visible chars only (exclude whitespace/punctuation noise).
    total = sum(1 for ch in text if ch.isalpha())
    if total == 0:
        return 0.0
    return native_chars / total


def _non_ascii_byte_ratio(text: str) -> float:
    """High non-ASCII byte rate with low native-script coverage indicates
    legacy ANSI/Bijoy font encoding (the SK Basic Training PDF case)."""
    if not text:
        return 0.0
    encoded = text.encode("utf-8", errors="replace")
    if not encoded:
        return 0.0
    non_ascii = sum(1 for b in encoded if b >= 0x80)
    return non_ascii / len(encoded)


def score_page(
    text: str,
    *,
    primary_language: str = "bn",
    is_multi_page_document: bool = True,
) -> QualityScore:
    """Score one page's text extraction. Returns QualityScore with pass/fail.

    `primary_language` should be the SourceDocument.primary_language. The
    encoding-integrity check fires for any language with a registered
    native script in `_NATIVE_SCRIPT_RANGES` (currently bn / bn_en_mixed /
    hi / mr / ta / te). For 'en' or other unregistered languages, only
    the text-empty check applies — Latin-script docs don't suffer the
    Bijoy/ANSI mojibake failure mode.
    """
    settings = get_settings()
    fail_reasons: list[str] = []
    text = text or ""

    # 1. Text-empty rate
    char_count = len(text.strip())
    if char_count < settings.extraction_quality_text_empty_min_chars:
        fail_reasons.append("text_empty")

    # 2. Encoding integrity (only for documents with a known native script)
    native_ratio = _native_script_ratio(text, primary_language)
    non_ascii_ratio = _non_ascii_byte_ratio(text)
    if primary_language in _NATIVE_SCRIPT_RANGES:
        # Fail when native-script coverage is below threshold AND non-ASCII
        # byte rate is above threshold (the signature of legacy ANSI/Bijoy
        # fonts: lots of bytes that look "non-ASCII" but aren't valid
        # native script — see the ASHA-NCDs-Hindi ingest where the doc
        # used Bijoy-Devanagari and the LLM silently dropped TB/FP modules
        # because canonical names appeared only as mojibake'd ASCII).
        if (
            native_ratio < settings.extraction_quality_native_codepoint_min
            and non_ascii_ratio > settings.extraction_quality_non_ascii_byte_max
        ):
            fail_reasons.append("native_encoding_corrupt")

    # 3. Heading absence (multi-page docs only — single-page doc may legitimately
    # have no heading)
    heading_count = len(_HEADING_RE.findall(text))
    # heading_count is per-page here; document-level heading absence is
    # checked by Stage B outline parser separately. We only fail extraction
    # when a multi-page doc's pages have no detectable structure AT ALL,
    # which is the vision-fallback signal.

    # Composite score: simple weighted average for triage UI.
    composite = (
        (1.0 if char_count >= settings.extraction_quality_text_empty_min_chars else 0.0) * 0.4
        + (1.0 if "native_encoding_corrupt" not in fail_reasons else 0.0) * 0.5
        + (min(heading_count, 3) / 3.0) * 0.1
    )

    return QualityScore(
        passed=len(fail_reasons) == 0,
        composite_score=composite,
        text_char_count=char_count,
        native_script_ratio=native_ratio,
        non_ascii_byte_ratio=non_ascii_ratio,
        heading_count=heading_count,
        fail_reasons=tuple(fail_reasons),
    )
