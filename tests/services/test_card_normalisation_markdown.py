"""Tests for markdown stripping in card persistence mapping."""

from __future__ import annotations

from platform_service.services.card_normalisation import card_dict_to_row_fields


def test_card_dict_to_row_fields_strips_markdown_in_localized_strings() -> None:
    card = {
        "title": {"bn": "__Title__ and _italic_"},
        "body": {"bn": "See [manual](https://example.com) and **bold** text"},
        "source_block_ids": [],
    }
    row = card_dict_to_row_fields(card)
    assert row["title_localized"] == {"bn": "Title and italic"}
    assert row["body_localized"] == {"bn": "See manual and bold text"}


def test_card_dict_to_row_fields_flattens_block_markdown() -> None:
    card = {
        "title": {"bn": "## Heading"},
        "body": {
            "bn": "\n".join(
                [
                    "- First **bold**",
                    "1. Second",
                    "",
                    "| a | b |",
                    "|---|---|",
                    "| 1 | 2 |",
                    "",
                    "```",
                    "code line",
                    "```",
                    "",
                    "> quoted",
                ]
            )
        },
        "source_block_ids": [],
    }
    row = card_dict_to_row_fields(card)
    assert row["title_localized"] == {"bn": "Heading"}
    assert row["body_localized"] == {"bn": "First bold\nSecond\na  b\n1  2\ncode line\nquoted"}
