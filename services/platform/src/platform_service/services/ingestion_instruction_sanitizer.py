"""Sanitize optional admin ingestion instructions before persistence and prompt use.

Deterministic guardrails against prompt injection and delimiter collision.
No LLM involvement — reject or normalize at the API boundary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_MAX_LINES = 50

# Case-insensitive blocklist for obvious override / injection attempts.
_BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
        r"disregard\s+(the\s+)?(ground\s+rules|grouping\s+rules|system)",
        r"you\s+are\s+now\s+(a|an)\b",
        r"forget\s+(your|the)\s+(rules|instructions|prompt)",
        r"\bsystem\s+prompt\b",
        r"override\s+(the\s+)?json\s+schema",
    )
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")


@dataclass(frozen=True)
class IngestionInstructionSanitizeResult:
    """Outcome of sanitizing optional ingestion instructions."""

    text: str | None
    rejected: bool
    rejection_reason: str | None


def sanitize_ingestion_instructions(
    raw: str | None,
    *,
    max_length: int,
) -> IngestionInstructionSanitizeResult:
    """Normalize and validate optional admin steering text.

    Returns ``text=None`` when input is absent or empty after sanitization.
    """
    if raw is None:
        return IngestionInstructionSanitizeResult(text=None, rejected=False, rejection_reason=None)

    normalized = unicodedata.normalize("NFKC", raw).strip()
    if not normalized:
        return IngestionInstructionSanitizeResult(text=None, rejected=False, rejection_reason=None)

    cleaned = _CONTROL_CHAR_RE.sub("", normalized)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    cleaned = _neutralize_section_headers(cleaned)
    cleaned = _collapse_horizontal_whitespace(cleaned).strip()
    if not cleaned:
        return IngestionInstructionSanitizeResult(text=None, rejected=False, rejection_reason=None)

    line_count = cleaned.count("\n") + 1
    if line_count > _MAX_LINES:
        return IngestionInstructionSanitizeResult(
            text=None,
            rejected=True,
            rejection_reason=f"ingestion instructions exceed maximum of {_MAX_LINES} lines",
        )

    if len(cleaned) > max_length:
        return IngestionInstructionSanitizeResult(
            text=None,
            rejected=True,
            rejection_reason=f"ingestion instructions exceed maximum length of {max_length} characters",
        )

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(cleaned):
            return IngestionInstructionSanitizeResult(
                text=None,
                rejected=True,
                rejection_reason="ingestion instructions contain disallowed override phrases",
            )

    return IngestionInstructionSanitizeResult(text=cleaned, rejected=False, rejection_reason=None)


def _neutralize_section_headers(text: str) -> str:
    """Prevent user text from mimicking prompt section delimiters."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("##"):
            prefix_len = len(line) - len(stripped)
            lines.append(f"{line[:prefix_len]}{stripped.removeprefix('##').lstrip()}")
        else:
            lines.append(line)
    return "\n".join(lines)


def _collapse_horizontal_whitespace(text: str) -> str:
    lines = [_HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(lines)
