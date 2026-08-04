"""Content equality helpers for idempotent admin module edits."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from mc_contracts.admin_modules import ModuleEditRequest, QuizQuestionEditRequest, QuizQuestionPayload
from mc_contracts.localized import LocalizedString
from pydantic import BaseModel

from platform_service.db.models.module import Module

# FE edit body sends title/description/module_json/thumbnail; quiz may be nested
# under module_json. chatbot_faqs_only is not edited by the dashboard and must not
# gate snapshot completeness or equality.
_COMPLETE_SNAPSHOT_FIELDS = frozenset(
    {
        "title",
        "description",
        "module_json",
        "thumbnail_storage_path",
    }
)

_CARD_NOISE_KEYS = frozenset(
    {
        "id",
        "card_family_id",
        "card_version",
        "source_pages",
        "presigned_url",
        "presigned_expires_seconds",
        "search_metadata",
    }
)

QuizEditItem = QuizQuestionEditRequest | QuizQuestionPayload | dict[str, Any]
QuizEditRequestItem = QuizQuestionEditRequest | dict[str, Any]


def resolve_edit_request_quiz(body: ModuleEditRequest) -> list[QuizEditRequestItem] | None:
    """Quiz from top-level body.quiz, else module_json.quiz (dashboard FE shape)."""
    if body.quiz is not None:
        # Widen list[QuizQuestionEditRequest] → list[QuizEditRequestItem] (list is invariant).
        return list[QuizEditRequestItem](body.quiz)
    if body.module_json is not None and "quiz" in body.module_json:
        raw = body.module_json.get("quiz")
        if raw is None:
            return []
        if isinstance(raw, list):
            return list[QuizEditRequestItem](raw)
        return None
    return None


def is_complete_edit_snapshot(body: ModuleEditRequest) -> bool:
    """True when every content field required for a no-op comparison is present.

    Quiz may be top-level or nested under ``module_json`` (analytics dashboard).
    """
    if not (_COMPLETE_SNAPSHOT_FIELDS <= body.model_fields_set):
        return False
    if "quiz" in body.model_fields_set:
        return True
    return resolve_edit_request_quiz(body) is not None


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(k): normalized
            for k, v in value.items()
            if (normalized := _json_ready(v)) not in (None, [], {})
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def _canonical_dump(value: Any) -> str:
    return json.dumps(_json_ready(value), sort_keys=True, ensure_ascii=False, default=str)


def _strip_card_noise(card: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in card.items() if k not in _CARD_NOISE_KEYS}


def _canonical_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_strip_card_noise(dict(card)) for card in cards if isinstance(card, dict)]


def _canonical_quiz_item(item: QuizEditItem) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        payload = item.model_dump(mode="json")
    else:
        payload = dict(item)
    payload.pop("id", None)
    payload.pop("question_family_id", None)
    payload.pop("question_version", None)
    return payload


def _canonical_quiz(quiz: Sequence[QuizEditItem] | None) -> list[dict[str, Any]]:
    if not quiz:
        return []
    return [_canonical_quiz_item(item) for item in quiz]


def _canonical_module_json_shell(module_json: dict[str, Any] | None) -> dict[str, Any]:
    shell = dict(module_json or {})
    attachments = shell.get("attachments")
    if not attachments:
        shell.pop("attachments", None)
    return shell


def canonical_edit_content(
    *,
    title: LocalizedString | None,
    description: LocalizedString | None,
    module_json_shell: dict[str, Any] | None,
    cards: list[dict[str, Any]],
    quiz: Sequence[QuizEditItem] | None,
    thumbnail_storage_path: str | None,
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "module_json": _canonical_module_json_shell(module_json_shell),
        "cards": _canonical_cards(cards),
        "quiz": _canonical_quiz(quiz),
        "thumbnail_storage_path": thumbnail_storage_path,
    }


def canonical_content_from_module(
    module: Module,
    *,
    cards: list[dict[str, Any]],
    quiz: Sequence[QuizQuestionPayload],
) -> dict[str, Any]:
    return canonical_edit_content(
        title=module.title_localized,
        description=module.description_localized,
        module_json_shell=module.module_json if isinstance(module.module_json, dict) else {},
        cards=cards,
        quiz=quiz,
        thumbnail_storage_path=module.thumbnail_storage_path,
    )


def edit_content_matches(
    *,
    request_title: LocalizedString | None,
    request_description: LocalizedString | None,
    request_module_json_shell: dict[str, Any] | None,
    request_cards: list[dict[str, Any]],
    request_quiz: Sequence[QuizEditItem] | None,
    request_thumbnail_storage_path: str | None,
    module: Module,
    current_cards: list[dict[str, Any]],
    current_quiz: Sequence[QuizQuestionPayload],
) -> bool:
    request_canonical = canonical_edit_content(
        title=request_title,
        description=request_description,
        module_json_shell=request_module_json_shell,
        cards=request_cards,
        quiz=request_quiz,
        thumbnail_storage_path=request_thumbnail_storage_path,
    )
    current_canonical = canonical_content_from_module(
        module,
        cards=current_cards,
        quiz=current_quiz,
    )
    return _canonical_dump(request_canonical) == _canonical_dump(current_canonical)
