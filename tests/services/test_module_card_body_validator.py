"""Tests for module_card_body_validator."""

from __future__ import annotations

import pytest
from platform_service.db.validators import ValidationError
from platform_service.services.module_card_body_validator import validate_module_card_bodies

_BLOCK_LIST_BODY = [
    {
        "type": "paragraph",
        "content": [{"type": "text", "text": "bn", "marks": [{"type": "bold"}]}],
    }
]


def test_accepts_string_and_prosemirror_bodies() -> None:
    module_json = {
        "cards": [
            {
                "title": {"bn": "কার্ড"},
                "body": {
                    "bn": "plain",
                    "en": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "en"}]}],
                    },
                },
            }
        ]
    }
    out = validate_module_card_bodies(module_json)
    assert out is not None
    assert out["cards"][0]["body"]["bn"] == "plain"


def test_accepts_block_list_and_single_block_dict() -> None:
    module_json = {
        "cards": [
            {
                "title": {"bn": "কার্ড"},
                "body": {
                    "bn": _BLOCK_LIST_BODY,
                    "en": {"type": "paragraph", "content": [{"type": "text", "text": "en"}]},
                },
            }
        ]
    }
    out = validate_module_card_bodies(module_json)
    assert out is not None
    assert out["cards"][0]["body"]["bn"] == _BLOCK_LIST_BODY


def test_rejects_invalid_body_shapes() -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_module_card_bodies({"cards": [{"title": {"bn": "কার্ড"}, "body": {"bn": {"foo": 1}}}]})
    assert exc_info.value.code == "invalid_card_body"

    with pytest.raises(ValidationError) as exc_info:
        validate_module_card_bodies({"cards": [{"title": {"bn": "কার্ড"}, "body": {"bn": [1, 2]}}]})
    assert exc_info.value.code == "invalid_card_body"
