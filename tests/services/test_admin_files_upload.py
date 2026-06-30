"""Unit tests for admin file streaming upload (no database required)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from platform_service.api.admin_files import _stream_uploadfile_to_path_capped


class _FakeUpload:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_stream_uploadfile_rejects_above_max_bytes(tmp_path: Path) -> None:
    upload = _FakeUpload([b"a" * 6, b"b" * 6])
    dest = tmp_path / "out.bin"

    with pytest.raises(HTTPException) as exc_info:
        await _stream_uploadfile_to_path_capped(upload, dest, max_bytes=10)

    assert exc_info.value.status_code == 413
    assert not dest.exists()


@pytest.mark.asyncio
async def test_stream_uploadfile_appends_multiple_chunks(tmp_path: Path) -> None:
    upload = _FakeUpload([b"a" * 6, b"b" * 6])
    dest = tmp_path / "out.bin"

    await _stream_uploadfile_to_path_capped(upload, dest, max_bytes=20)

    assert dest.read_bytes() == b"a" * 6 + b"b" * 6
