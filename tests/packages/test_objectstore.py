"""Unit tests for mc_foundation.objectstore."""

from __future__ import annotations

from pathlib import Path

import pytest
from mc_foundation.objectstore import (
    InMemoryObjectStore,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectTooLargeError,
    looks_like_object_storage_storage_path,
    resolve_object_name,
    safe_basename,
)

_PREFIXES = frozenset({"ingest", "uploads", "media", "source-documents"})


class TestHelpers:
    def test_looks_like_storage_path(self) -> None:
        assert looks_like_object_storage_storage_path(
            "medtronics-storage/ingest/a.pdf", bucket_name="medtronics-storage"
        )
        assert not looks_like_object_storage_storage_path("ingest/a.pdf", bucket_name="medtronics-storage")

    def test_safe_basename(self) -> None:
        assert safe_basename("path/to/manual.pdf") == "manual.pdf"
        with pytest.raises(ValueError, match="filename is required"):
            safe_basename("///")

    def test_resolve_strips_configured_bucket_prefix(self) -> None:
        assert (
            resolve_object_name(
                "medtronics-storage/ingest/a.pdf",
                bucket_name="medtronics-storage",
                allowed_prefixes=_PREFIXES,
            )
            == "ingest/a.pdf"
        )

    def test_resolve_accepts_key_without_bucket(self) -> None:
        assert (
            resolve_object_name(
                "ingest/a.pdf",
                bucket_name="medtronics-storage",
                allowed_prefixes=_PREFIXES,
            )
            == "ingest/a.pdf"
        )

    def test_resolve_rejects_foreign_bucket(self) -> None:
        with pytest.raises(ValueError, match="other-bucket"):
            resolve_object_name(
                "other-bucket/ingest/a.pdf",
                bucket_name="medtronics-storage",
                allowed_prefixes=_PREFIXES,
            )

    def test_resolve_rejects_path_traversal(self) -> None:
        with pytest.raises(ValueError, match="unsafe path segment"):
            resolve_object_name(
                "ingest/../etc/passwd",
                bucket_name="medtronics-storage",
                allowed_prefixes=_PREFIXES,
            )

    def test_resolve_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="object_name is required"):
            resolve_object_name(
                "",
                bucket_name="medtronics-storage",
                allowed_prefixes=_PREFIXES,
            )


class TestInMemoryObjectStore:
    @pytest.mark.asyncio
    async def test_upload_download_roundtrip(self, tmp_path: Path) -> None:
        store = InMemoryObjectStore(
            bucket_name="test-bucket",
            allowed_prefixes=frozenset({"uploads"}),
        )
        from io import BytesIO

        stored = await store.upload_file(
            file_obj=BytesIO(b"hello"),
            filename="note.txt",
            prefix="uploads",
        )
        assert stored.storage_path.startswith("test-bucket/uploads/")
        dest = tmp_path / "out.txt"
        await store.download_storage_path_to_local_file(storage_path=stored.storage_path, dest_path=dest)
        assert dest.read_bytes() == b"hello"

    @pytest.mark.asyncio
    async def test_upload_rejects_oversized(self) -> None:
        store = InMemoryObjectStore(allowed_prefixes=frozenset({"uploads"}))
        from io import BytesIO

        with pytest.raises(ObjectTooLargeError):
            await store.upload_file(
                file_obj=BytesIO(b"abcdef"),
                filename="big.bin",
                prefix="uploads",
                max_bytes=3,
            )

    @pytest.mark.asyncio
    async def test_stat_and_presign_missing(self) -> None:
        store = InMemoryObjectStore(allowed_prefixes=frozenset({"uploads"}))
        with pytest.raises(ObjectNotFoundError):
            await store.stat_object("uploads/missing.txt")
        with pytest.raises(ObjectNotFoundError):
            await store.presigned_get_url(object_name="uploads/missing.txt", expires_seconds=60)

    @pytest.mark.asyncio
    async def test_put_from_local_and_presign(self, tmp_path: Path) -> None:
        store = InMemoryObjectStore(allowed_prefixes=frozenset({"ingest"}))
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"%PDF")
        stored = await store.put_object_from_local_file(
            object_name="ingest/doc.pdf",
            local_path=src,
            content_type="application/pdf",
            metadata={"sha256": "abc"},
        )
        assert stored.object_name == "ingest/doc.pdf"
        await store.stat_object("ingest/doc.pdf")
        url = await store.presigned_get_url(object_name="ingest/doc.pdf", expires_seconds=120)
        assert url.url == "memory://test-bucket/ingest/doc.pdf"
        assert url.expires_seconds == 120

    @pytest.mark.asyncio
    async def test_readiness_requires_auto_create_or_ready(self) -> None:
        store = InMemoryObjectStore(auto_create_bucket=False)
        with pytest.raises(ObjectStorageError, match="does not exist"):
            await store.check_readiness()
        store = InMemoryObjectStore(auto_create_bucket=True)
        await store.check_readiness()
