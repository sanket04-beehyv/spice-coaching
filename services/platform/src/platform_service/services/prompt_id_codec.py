"""Short-token ↔ UUID translation for the Stage 2 prompt.

Gemini 2.5 Pro mis-transcribes UUIDs at scale: `cbf75928-…` came back as
`cbf7928-…` (digit dropped) on a 35K-token chunk with ~70 page UUIDs to
cite. UUIDs are 10-15 tokens of effectively-random hex; the model has no
semantic anchor to verify each character.

The fix is to never show the LLM a real UUID. The corpus is rendered with
short ordinal tokens (`d1`, `p47`, `b231`) which the model can copy
correctly because each token is 2-4 characters that read as a single
chunk. After the response comes back, every token in the candidate's
`source_provenance` is translated back to its real UUID before the
existing membership validation runs.

Numbering is global within one identification call — every page in every
document gets a unique `p{n}`, every block gets `b{n}`. This keeps
tokens short (max ~p168 / b3000 for a 168-page manual) and avoids
ambiguity across documents in mixed-corpus calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptIdCodec:
    """Bidirectional UUID ↔ short-token map for one Stage 2 call."""

    _doc_to_token: dict[str, str] = field(default_factory=dict)
    _page_to_token: dict[str, str] = field(default_factory=dict)
    _block_to_token: dict[str, str] = field(default_factory=dict)
    _token_to_doc: dict[str, str] = field(default_factory=dict)
    _token_to_page: dict[str, str] = field(default_factory=dict)
    _token_to_block: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_corpus(cls, page_corpus: list[dict[str, Any]]) -> PromptIdCodec:
        """Build a codec covering every doc/page/block UUID in the corpus."""
        codec = cls()
        for doc in page_corpus:
            doc_uuid = str(doc.get("source_document_id", ""))
            if doc_uuid and doc_uuid not in codec._doc_to_token:
                token = f"d{len(codec._doc_to_token) + 1}"
                codec._doc_to_token[doc_uuid] = token
                codec._token_to_doc[token] = doc_uuid
            for page in doc.get("pages", []) or []:
                page_uuid = str(page.get("source_page_id", ""))
                if page_uuid and page_uuid not in codec._page_to_token:
                    token = f"p{len(codec._page_to_token) + 1}"
                    codec._page_to_token[page_uuid] = token
                    codec._token_to_page[token] = page_uuid
                for block in page.get("blocks", []) or []:
                    block_uuid = str(block.get("content_block_id", ""))
                    if block_uuid and block_uuid not in codec._block_to_token:
                        token = f"b{len(codec._block_to_token) + 1}"
                        codec._block_to_token[block_uuid] = token
                        codec._token_to_block[token] = block_uuid
        return codec

    # ── Encode (UUID → token; used when rendering the prompt) ────────
    def doc_token(self, uuid_str: str) -> str:
        return self._doc_to_token[str(uuid_str)]

    def page_token(self, uuid_str: str) -> str:
        return self._page_to_token[str(uuid_str)]

    def block_token(self, uuid_str: str) -> str:
        return self._block_to_token[str(uuid_str)]

    # ── Decode (token → UUID; used when parsing the LLM response) ────
    @staticmethod
    def _norm(token: Any) -> str:
        return str(token or "").strip().lower()

    def decode_doc(self, token: Any) -> str | None:
        return self._token_to_doc.get(self._norm(token))

    def decode_page(self, token: Any) -> str | None:
        return self._token_to_page.get(self._norm(token))

    def decode_block(self, token: Any) -> str | None:
        return self._token_to_block.get(self._norm(token))

    # ── Stats (logging / debugging) ──────────────────────────────────
    @property
    def doc_count(self) -> int:
        return len(self._doc_to_token)

    @property
    def page_count(self) -> int:
        return len(self._page_to_token)

    @property
    def block_count(self) -> int:
        return len(self._block_to_token)


def translate_provenance_tokens(
    candidate: dict[str, Any],
    codec: PromptIdCodec,
) -> int:
    """Best-effort: replace short tokens (`d1`, `p47`, `b231`) with real
    UUIDs in `candidate["source_provenance"]`. Mutates in place.

    Returns the count of tokens successfully translated.

    Tokens that don't decode (hallucinated or stale) are left as-is. The
    downstream UUID-format and `valid_*_ids` membership checks in
    `_validate_candidate` remain the authoritative gate — if a value
    isn't a UUID after translation, or is a UUID not in the corpus, the
    validator rejects it with the existing warning.
    """
    provenance = candidate.get("source_provenance")
    if not isinstance(provenance, list):
        return 0

    decoded_count = 0
    for entry in provenance:
        if not isinstance(entry, dict):
            continue

        doc_token = entry.get("source_document_id")
        if doc_token is not None:
            real = codec.decode_doc(doc_token)
            if real is not None:
                entry["source_document_id"] = real
                decoded_count += 1

        page_token = entry.get("source_page_id")
        if page_token is not None:
            real = codec.decode_page(page_token)
            if real is not None:
                entry["source_page_id"] = real
                decoded_count += 1

        block_tokens = entry.get("content_block_ids")
        if isinstance(block_tokens, list):
            decoded_blocks: list[Any] = []
            for tok in block_tokens:
                real = codec.decode_block(tok)
                if real is not None:
                    decoded_blocks.append(real)
                    decoded_count += 1
                else:
                    decoded_blocks.append(tok)  # leave for validator
            entry["content_block_ids"] = decoded_blocks

    return decoded_count
