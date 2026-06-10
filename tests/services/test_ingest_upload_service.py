"""Unit tests for ingest upload helpers (no database required)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from platform_service.services.ingest_upload_service import IngestUploadService


class _FakeUpload:
    def __init__(self, filename: str) -> None:
        self.filename = filename


def test_resolve_override_duplicates_defaults_to_false() -> None:
    uploads = [_FakeUpload("a.pdf"), _FakeUpload("b.pdf")]
    assert IngestUploadService.resolve_override_duplicates_for_files(None, uploads) == [False, False]


def test_resolve_override_duplicates_parses_json_array() -> None:
    uploads = [_FakeUpload("a.pdf"), _FakeUpload("b.pdf")]
    assert IngestUploadService.resolve_override_duplicates_for_files("[true, false]", uploads) == [
        True,
        False,
    ]


def test_resolve_override_duplicates_rejects_length_mismatch() -> None:
    uploads = [_FakeUpload("a.pdf"), _FakeUpload("b.pdf")]
    with pytest.raises(HTTPException) as exc_info:
        IngestUploadService.resolve_override_duplicates_for_files("[true]", uploads)
    assert exc_info.value.status_code == 400
    assert "2 entries" in exc_info.value.detail


def test_resolve_override_duplicates_rejects_non_boolean_entry() -> None:
    uploads = [_FakeUpload("a.pdf")]
    with pytest.raises(HTTPException) as exc_info:
        IngestUploadService.resolve_override_duplicates_for_files('["yes"]', uploads)
    assert exc_info.value.status_code == 400
    assert "boolean" in exc_info.value.detail
