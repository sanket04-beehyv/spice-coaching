"""Tests for card_body_text helpers."""

from __future__ import annotations

from platform_service.services.card_body_text import (
    card_body_char_len,
    card_body_is_nonempty,
    card_body_plain_text,
    is_prosemirror_doc,
    is_rich_text_blocks,
    is_rich_text_body,
)


def test_is_prosemirror_doc() -> None:
    assert is_prosemirror_doc({"type": "doc", "content": []})
    assert not is_prosemirror_doc("plain")
    assert not is_prosemirror_doc({"type": "paragraph", "content": []})


def test_plain_string_body() -> None:
    assert card_body_plain_text("  hello  ") == "hello"
    assert card_body_plain_text("**bold** text") == "bold text"
    assert card_body_is_nonempty("hello")
    assert not card_body_is_nonempty("   ")
    assert card_body_char_len("abcd") == 4


def test_empty_prosemirror_doc() -> None:
    doc = {"type": "doc", "content": []}
    assert card_body_plain_text(doc) == ""
    assert not card_body_is_nonempty(doc)


def test_prosemirror_paragraphs() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "রক্তচাপ পরিমাপ করুন।"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Bold part",
                        "marks": [{"type": "bold"}],
                    }
                ],
            },
        ],
    }
    assert card_body_plain_text(doc) == "রক্তচাপ পরিমাপ করুন।\nBold part"
    assert card_body_is_nonempty(doc)
    assert card_body_char_len(doc) == len("রক্তচাপ পরিমাপ করুন।\nBold part")


def test_prosemirror_list_and_hard_break() -> None:
    doc = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "One"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Two"},
                                    {"type": "hardBreak"},
                                    {"type": "text", "text": "lines"},
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    assert card_body_plain_text(doc) == "One\nTwo\nlines"


def test_unknown_dict_harvests_text() -> None:
    value = {"type": "custom", "content": [{"type": "text", "text": "fallback"}]}
    assert card_body_plain_text(value) == "fallback"


def test_none_and_non_string_scalar() -> None:
    assert card_body_plain_text(None) == ""
    assert card_body_plain_text(42) == "42"


def test_is_rich_text_body_shapes() -> None:
    block = {"type": "paragraph", "content": [{"type": "text", "text": "x"}]}
    assert is_rich_text_body(block)
    assert is_rich_text_body([block])
    assert is_rich_text_body({"type": "doc", "content": []})
    assert not is_rich_text_body("plain")
    assert not is_rich_text_body({"foo": 1})
    assert is_rich_text_blocks([])
    assert not is_rich_text_blocks([{"foo": 1}])


def test_rich_text_block_list_with_bold_marks() -> None:
    blocks = [
        {
            "type": "paragraph",
            "content": [
                {
                    "text": "সেপসিস মানে সংক্রমণ।",
                    "type": "text",
                    "marks": [{"type": "bold"}],
                }
            ],
        }
    ]
    assert card_body_plain_text(blocks) == "সেপসিস মানে সংক্রমণ।"
    assert card_body_is_nonempty(blocks)


def test_single_block_dict_body() -> None:
    block = {
        "type": "paragraph",
        "content": [{"type": "text", "text": "Single block"}],
    }
    assert card_body_plain_text(block) == "Single block"
    assert card_body_is_nonempty(block)


def test_empty_block_list() -> None:
    assert card_body_plain_text([]) == ""
    assert not card_body_is_nonempty([])
