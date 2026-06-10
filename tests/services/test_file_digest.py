"""Sanity tests for streaming sha256."""

import hashlib

import pytest
from platform_service.services.file_digest import sha256_hex_file


def test_sha256_matches_hashlib(tmp_path):
    payload = b"the quick brown fox" * 50_000  # ~1 MB, exercises the chunk loop
    file = tmp_path / "blob.bin"
    file.write_bytes(payload)
    assert sha256_hex_file(file) == hashlib.sha256(payload).hexdigest()


def test_sha256_empty_file(tmp_path):
    file = tmp_path / "empty.bin"
    file.write_bytes(b"")
    assert sha256_hex_file(file) == hashlib.sha256(b"").hexdigest()


def test_sha256_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        sha256_hex_file(tmp_path / "nope.bin")
