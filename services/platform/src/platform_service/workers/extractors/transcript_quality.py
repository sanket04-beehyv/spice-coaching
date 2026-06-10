"""Composite quality score for AV transcripts — observational, not protective.

Replaces the previous ``extraction_quality_score=1.0`` hardcode so each
transcript chunk lands with a real number on
``source_page.extraction_quality_score``. The number is recorded for
audit + dashboard rollup; the candidate-level
``insufficient_source_filter`` does NOT consume it today (it gates on
token count and heading count, which already catches the obvious
silence/empty-chunk case).

What this heuristic can detect:

- Empty / silence chunks (caught primarily by token count downstream;
  this scores them 0.0 as a parallel signal).
- Mumble / mostly-fillers — short transcript or very short avg token
  length scores 0.2-0.6.

What this heuristic CANNOT detect:

- Fluent hallucinations (long, normal-shaped tokens that say nothing).
- Language drift (model returned coherent garbage in the wrong language).

Those cases need a semantic check the heuristic cannot do. Until a
semantic signal is available, treat the score as observational only.
"""


def compute_transcript_quality(text: str) -> float:
    """Composite quality score for a chunk transcript (see module docstring)."""
    cleaned = text.strip()
    if not cleaned:
        return 0.0
    words = cleaned.split()
    word_count = len(words)
    if word_count < 5:
        return 0.2
    avg_word_len = sum(len(w) for w in words) / word_count
    if word_count < 20:
        return 0.5
    if avg_word_len < 2.5:
        return 0.6
    return 0.85
