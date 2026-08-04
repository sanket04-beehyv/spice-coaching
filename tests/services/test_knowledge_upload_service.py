"""Unit tests for KnowledgeUploadService parsing helpers."""

from __future__ import annotations

import pytest
from platform_service.services.knowledge_upload_service import (
    KnowledgeUploadService,
    KnowledgeValidationError,
)


def test_parse_splits_empty_modes() -> None:
    assert KnowledgeUploadService.parse_splits(None) == []
    assert KnowledgeUploadService.parse_splits("") == []
    assert KnowledgeUploadService.parse_splits("[]") == []


def test_parse_splits_valid() -> None:
    splits = KnowledgeUploadService.parse_splits(
        '[{"start_page": 1, "end_page": 2, "title": "A", "thumbnail_storage_path": null}]'
    )
    assert len(splits) == 1
    assert splits[0].start_page == 1
    assert splits[0].end_page == 2
    assert splits[0].title == "A"


def test_parse_splits_rejects_inverted_range() -> None:
    with pytest.raises(KnowledgeValidationError, match="start_page"):
        KnowledgeUploadService.parse_splits('[{"start_page": 3, "end_page": 1, "title": "Bad"}]')


def test_parse_splits_rejects_invalid_json() -> None:
    with pytest.raises(KnowledgeValidationError, match="valid JSON"):
        KnowledgeUploadService.parse_splits("{not-json")
