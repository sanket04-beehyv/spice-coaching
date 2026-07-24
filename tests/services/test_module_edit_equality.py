"""Unit tests for admin module edit snapshot equality helpers."""

from __future__ import annotations

from mc_contracts.admin_modules import ModuleEditRequest
from platform_service.services.module_edit_equality import (
    is_complete_edit_snapshot,
    resolve_edit_request_quiz,
)


def test_complete_snapshot_requires_core_fields_and_quiz() -> None:
    incomplete = ModuleEditRequest(
        expected_version=1,
        title={"bn": "t"},
        description={"bn": "d"},
        module_json={"cards": []},
        thumbnail_storage_path=None,
    )
    assert is_complete_edit_snapshot(incomplete) is False

    nested = ModuleEditRequest(
        expected_version=1,
        title={"bn": "t"},
        description={"bn": "d"},
        module_json={"cards": [], "quiz": []},
        thumbnail_storage_path=None,
    )
    assert is_complete_edit_snapshot(nested) is True
    assert resolve_edit_request_quiz(nested) == []

    top_level = ModuleEditRequest(
        expected_version=1,
        title={"bn": "t"},
        description={"bn": "d"},
        module_json={"cards": []},
        quiz=[],
        thumbnail_storage_path=None,
    )
    assert is_complete_edit_snapshot(top_level) is True


def test_chatbot_faqs_only_is_not_required_for_complete_snapshot() -> None:
    body = ModuleEditRequest(
        expected_version=1,
        title={"bn": "t"},
        description={"bn": "d"},
        module_json={"cards": [], "quiz": []},
        thumbnail_storage_path="/path/thumb.png",
    )
    assert "chatbot_faqs_only" not in body.model_fields_set
    assert is_complete_edit_snapshot(body) is True
