from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from platform_service.services.ingest_upload_service import (
    IngestUploadService,
    stream_upload_to_path,
)


def test_source_type_from_suffix_supports_audio_video() -> None:
    assert IngestUploadService.source_type_from_suffix(".mp3") == "audio"
    assert IngestUploadService.source_type_from_suffix(".wav") == "audio"
    assert IngestUploadService.source_type_from_suffix(".mp4") == "video"
    assert IngestUploadService.source_type_from_suffix(".mov") == "video"


class _FakeUpload:
    def __init__(self, filename: str, chunks: list[bytes]) -> None:
        self.filename = filename
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_stream_upload_rejects_media_above_limit(tmp_path: Path) -> None:
    upload = _FakeUpload("large.mp3", [b"a" * 6, b"b" * 6])
    dest = tmp_path / "out.bin"

    with pytest.raises(HTTPException) as exc_info:
        await stream_upload_to_path(upload, dest, source_type="audio", max_media_bytes=10)

    assert exc_info.value.status_code == 413
    assert not dest.exists()


@pytest.mark.asyncio
async def test_stream_upload_allows_document_above_media_limit(tmp_path: Path) -> None:
    upload = _FakeUpload("large.pdf", [b"a" * 6, b"b" * 6])
    dest = tmp_path / "out.pdf"

    await stream_upload_to_path(upload, dest, source_type="pdf", max_media_bytes=10)

    assert dest.read_bytes() == b"a" * 6 + b"b" * 6


def test_resolve_titles_defaults_to_filename_stems() -> None:
    uploads = [_FakeUpload("BRAC Guide.pdf", []), _FakeUpload("uhis-workflow.pptx", [])]
    assert IngestUploadService.resolve_titles_for_files(None, uploads) == ["BRAC Guide", "uhis-workflow"]


def test_resolve_titles_parses_json_array() -> None:
    uploads = [_FakeUpload("a.pdf", []), _FakeUpload("b.pdf", [])]
    assert IngestUploadService.resolve_titles_for_files('["First","Second"]', uploads) == [
        "First",
        "Second",
    ]


def test_resolve_titles_rejects_length_mismatch() -> None:
    uploads = [_FakeUpload("a.pdf", []), _FakeUpload("b.pdf", [])]
    with pytest.raises(HTTPException) as exc_info:
        IngestUploadService.resolve_titles_for_files('["Only one"]', uploads)
    assert exc_info.value.status_code == 400
    assert "2 entries" in exc_info.value.detail


def test_resolve_titles_rejects_invalid_json() -> None:
    uploads = [_FakeUpload("a.pdf", [])]
    with pytest.raises(HTTPException) as exc_info:
        IngestUploadService.resolve_titles_for_files("not-json", uploads)
    assert exc_info.value.status_code == 400


def test_resolve_titles_rejects_empty_string_entry() -> None:
    uploads = [_FakeUpload("a.pdf", [])]
    with pytest.raises(HTTPException) as exc_info:
        IngestUploadService.resolve_titles_for_files('[""]', uploads)
    assert exc_info.value.status_code == 400
