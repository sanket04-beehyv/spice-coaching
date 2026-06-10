"""W-3 Stage B — deterministic markdown-headings outline parser.

Per Pipeline v3.3 §5. Walks all source_page.markdown_content concatenated,
tracks per-page heading occurrences, and builds a section tree with
page_range covering each section's span.

Code blocks (fenced ``` ... ```) are respected — heading-like text inside
fenced blocks is NOT treated as a heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from platform_service.services.llm_text_utils import is_fence_line

# Markdown heading: 1–6 '#' followed by space, then heading text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class HeadingHit:
    """One heading detected in the corpus."""

    page_number: int  # 1-indexed within the source_document
    level: int  # 1..6 (number of '#')
    text: str
    line_index: int  # 0-indexed line within the page's markdown


@dataclass
class OutlineSection:
    """One section in the outline tree."""

    section_id: str
    level: int
    heading: str
    page_range: tuple[int, int]  # (start_page, end_page), 1-indexed inclusive
    topics: list[str] = field(default_factory=list)
    subsections: list[OutlineSection] = field(default_factory=list)

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "level": self.level,
            "heading": self.heading,
            "page_range": list(self.page_range),
            "topics": list(self.topics),
            "subsections": [s.to_jsonb() for s in self.subsections],
        }


@dataclass
class ParsedOutline:
    """Result of the markdown outline parser."""

    sections: list[OutlineSection]
    heading_hits: list[HeadingHit]
    pages_with_heading: set[int]
    total_pages: int
    document_title_inferred: str | None
    primary_language: str | None

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "document_title_inferred": self.document_title_inferred,
            "primary_language": self.primary_language,
            "sections": [s.to_jsonb() for s in self.sections],
        }


# ── Heading detection ───────────────────────────────────────────────────


def find_heading_hits(pages: list[tuple[int, str]]) -> list[HeadingHit]:
    """Detect markdown headings across pages, ignoring fenced code blocks.

    `pages` is a list of (page_number, markdown_content) tuples in order.
    Returns headings in document order.
    """
    hits: list[HeadingHit] = []
    for page_number, markdown in pages:
        if not markdown:
            continue
        in_fence = False
        for line_index, line in enumerate(markdown.splitlines()):
            if is_fence_line(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _HEADING_RE.match(line)
            if m:
                hashes, text = m.group(1), m.group(2).strip()
                if not text:
                    continue
                hits.append(
                    HeadingHit(
                        page_number=page_number,
                        level=len(hashes),
                        text=text,
                        line_index=line_index,
                    )
                )
    return hits


# ── Topic term extraction ───────────────────────────────────────────────

# English noun-phrase-ish: capitalised words and acronyms.
_ENGLISH_NP_RE = re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\b")
# Bangla word boundary on whitespace (Bangla doesn't use word case).
_WHITESPACE_RE = re.compile(r"\s+")


def extract_topic_terms(text: str, *, language: str | None, max_terms: int = 5) -> list[str]:
    """Extract candidate topic terms from a paragraph.

    English: capitalised noun phrases + 3-letter acronyms (BP, ANC, IFA).
    Bangla: first N unique whitespace-separated tokens of length ≥ 3.
    Returns at most `max_terms` unique entries in encounter order.
    """
    text = (text or "").strip()
    if not text:
        return []

    if language and language.startswith("bn"):
        seen: list[str] = []
        for tok in _WHITESPACE_RE.split(text):
            tok = tok.strip(" ।,.;:!?'\"()[]{}")
            if len(tok) >= 3 and tok not in seen:
                seen.append(tok)
            if len(seen) >= max_terms:
                break
        return seen

    # English (or unknown — fall through)
    seen_en: list[str] = []
    for m in _ENGLISH_NP_RE.finditer(text):
        phrase = m.group(0)
        # Skip leading single-word artefacts that are just sentence starts ("This", "The").
        if phrase in {"This", "The", "A", "An", "It", "We", "Our", "Their"}:
            continue
        if phrase not in seen_en:
            seen_en.append(phrase)
        if len(seen_en) >= max_terms:
            break
    return seen_en


def first_paragraph_under(markdown: str, after_line_index: int) -> str:
    """Return the first non-empty, non-heading paragraph after a given line.

    Used for topic extraction ("what's the body immediately below this heading?").
    Stops at the next heading so each section gets its OWN body's terms.
    """
    lines = markdown.splitlines()
    paragraph_lines: list[str] = []
    started = False
    in_fence = False
    for idx, line in enumerate(lines):
        if idx <= after_line_index:
            continue
        if is_fence_line(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            if started:
                break
            continue
        if _HEADING_RE.match(line):
            break
        started = True
        paragraph_lines.append(line.strip())
        if len(" ".join(paragraph_lines)) > 400:
            break
    return " ".join(paragraph_lines)


def first_paragraph_of_page(markdown: str) -> str:
    """Return the first non-empty body paragraph anywhere on the page.

    Skips headings instead of stopping on them. Used for the LLM-outline-fallback
    prompt builder where we want representative page content regardless of
    where headings appear.
    """
    lines = markdown.splitlines()
    paragraph_lines: list[str] = []
    started = False
    in_fence = False
    for line in lines:
        if is_fence_line(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            if started:
                break
            continue
        if _HEADING_RE.match(line):
            # Skip headings; keep looking for body.
            if started:
                break
            continue
        started = True
        paragraph_lines.append(line.strip())
        if len(" ".join(paragraph_lines)) > 400:
            break
    return " ".join(paragraph_lines)


# ── Tree construction ───────────────────────────────────────────────────


def build_section_tree(
    hits: list[HeadingHit],
    pages: list[tuple[int, str]],
    total_pages: int,
    *,
    primary_language: str | None,
) -> list[OutlineSection]:
    """Build a hierarchical OutlineSection tree from a flat heading-hit list.

    Each section's page_range is `(this_heading.page, next_heading_at_same_or_higher_level.page - 1)`,
    bounded by the document's total page count.
    """
    if not hits:
        return []

    pages_by_number = {p: m for p, m in pages}

    # Compute end-page for each hit by scanning forward to the next hit with
    # level <= current level (sibling or ancestor heading).
    end_pages: list[int] = []
    for i, hit in enumerate(hits):
        end_page = total_pages
        for j in range(i + 1, len(hits)):
            if hits[j].level <= hit.level:
                # Section ends on the page just before the sibling/ancestor heading.
                end_page = max(hit.page_number, hits[j].page_number - 1)
                break
        end_pages.append(end_page)

    # Build section objects with topic terms from the first paragraph below.
    sections_flat: list[OutlineSection] = []
    parent_stack: list[OutlineSection] = []  # stack of currently-open ancestors

    for i, hit in enumerate(hits):
        # Pop the parent_stack until top has level < this.level
        while parent_stack and parent_stack[-1].level >= hit.level:
            parent_stack.pop()

        # Section id is dotted by depth: "s1", "s1.1", "s1.1.1", etc.
        if parent_stack:
            parent_id = parent_stack[-1].section_id
            sibling_count = len(parent_stack[-1].subsections) + 1
            section_id = f"{parent_id}.{sibling_count}"
        else:
            # Top-level section
            top_level_count = sum(1 for s in sections_flat if not s.section_id.count("."))
            section_id = f"s{top_level_count + 1}"

        # Topic terms from first paragraph below this heading on the same page.
        page_md = pages_by_number.get(hit.page_number, "")
        first_para = first_paragraph_under(page_md, hit.line_index)
        topics = extract_topic_terms(first_para, language=primary_language)

        section = OutlineSection(
            section_id=section_id,
            level=hit.level,
            heading=hit.text,
            page_range=(hit.page_number, end_pages[i]),
            topics=topics,
            subsections=[],
        )
        if parent_stack:
            parent_stack[-1].subsections.append(section)
        else:
            sections_flat.append(section)
        parent_stack.append(section)

    return sections_flat


# ── Public entry point ──────────────────────────────────────────────────


def parse_outline(
    pages: list[tuple[int, str]],
    *,
    total_pages: int,
    primary_language: str | None = None,
) -> ParsedOutline:
    """Parse a document's markdown into a heading-based outline.

    `pages` is the source_page rows ordered by page_number, as
    `(page_number, markdown_content)` tuples. `total_pages` should equal
    `len(pages)` for a fully-extracted document; pass explicitly so the
    parser doesn't need to depend on the SourcePage model.
    """
    hits = find_heading_hits(pages)
    pages_with_heading = {h.page_number for h in hits}
    sections = build_section_tree(hits, pages, total_pages, primary_language=primary_language)
    # Inferred document title: first H1 if present and lone-on-page-1 ish.
    title: str | None = None
    for h in hits:
        if h.level == 1:
            title = h.text
            break
    return ParsedOutline(
        sections=sections,
        heading_hits=hits,
        pages_with_heading=pages_with_heading,
        total_pages=total_pages,
        document_title_inferred=title,
        primary_language=primary_language,
    )
