"""Vendor-agnostic object store protocol and test double.

Production adapters (e.g. boto3 S3 / MinIO) live in the consuming service.
This module stays free of vendor SDKs.
"""

from __future__ import annotations

import io
import mimetypes
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

ContentDisposition = Literal["auto", "inline", "attachment"]


class ObjectStorageError(RuntimeError):
    """Raised when object storage cannot complete an operation."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when the requested object does not exist."""


class ObjectTooLargeError(ObjectStorageError):
    """Raised when an upload exceeds the configured size limit."""


@dataclass(frozen=True)
class StoredObject:
    bucket_name: str
    object_name: str
    storage_path: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class PresignedObjectUrl:
    url: str
    bucket_name: str
    object_name: str
    expires_seconds: int


def looks_like_object_storage_storage_path(ref: str, *, bucket_name: str) -> bool:
    """True when ``ref`` is stored as ``{bucket_name}/{object_key}``."""
    return str(ref).strip().startswith(f"{bucket_name}/")


def safe_basename(filename: str) -> str:
    """Last path segment of ``filename``, safe for object keys."""
    basename = filename.replace("\\", "/").split("/")[-1].strip()
    if not basename:
        raise ValueError("filename is required")
    return basename


def normalise_prefix(prefix: str) -> str:
    """Normalise an upload prefix; empty becomes ``uploads``."""
    clean = prefix.strip().strip("/")
    if not clean:
        return "uploads"

    parts = clean.split("/")
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise ValueError("prefix contains an unsafe path segment")
    return "/".join(parts)


def resolve_object_name(
    object_reference: str,
    *,
    bucket_name: str,
    allowed_prefixes: frozenset[str],
) -> str:
    """Normalise ``bucket/key`` or ``key`` to an object key within ``bucket_name``.

    If the reference starts with a bucket name segment:
    - It **must** match ``bucket_name``; otherwise ``ValueError`` is raised
      (avoids silently reading the wrong bucket after a misconfigured rename).
    - If it matches, the bucket prefix is stripped and the remainder is validated.
    If there is no bucket prefix, the first path segment must be an allowed prefix.
    """
    clean = object_reference.strip().lstrip("/")
    if not clean:
        raise ValueError("object_name is required")

    first, sep, rest = clean.partition("/")
    if sep:
        if first == bucket_name:
            clean = rest
        elif first not in allowed_prefixes:
            raise ValueError(
                f"object storage reference starts with {first!r}, which is neither "
                f"this service bucket ({bucket_name!r}) nor an allowed object prefix "
                f"({sorted(allowed_prefixes)})"
            )

    if not clean:
        raise ValueError("object_name is required")
    parts = clean.split("/")
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise ValueError("object reference contains an unsafe path segment")
    object_prefix = clean.split("/", maxsplit=1)[0]
    if object_prefix not in allowed_prefixes:
        raise ValueError(f"unsupported object prefix {object_prefix!r}; accepted: {sorted(allowed_prefixes)}")
    return clean


def content_type_for(object_name: str, fallback: str = "application/octet-stream") -> str:
    """Guess a MIME type from an object key basename."""
    return mimetypes.guess_type(object_name)[0] or fallback


class ObjectStore(Protocol):
    """Minimal durable object upload / download / presign surface."""

    bucket_name: str
    allowed_prefixes: frozenset[str]

    def close(self) -> None:
        """Release underlying client resources."""
        ...

    async def upload_file(
        self,
        *,
        file_obj: BinaryIO,
        filename: str,
        prefix: str = "uploads",
        max_bytes: int | None = None,
    ) -> StoredObject:
        """Upload bytes under a generated key within ``prefix``."""
        ...

    async def stat_object(self, object_name: str) -> None:
        """Verify an object exists in the configured bucket."""
        ...

    async def presigned_get_url(
        self,
        *,
        object_name: str,
        expires_seconds: int,
        disposition: ContentDisposition = "auto",
        download_filename: str | None = None,
    ) -> PresignedObjectUrl:
        """Build a time-limited GET URL for ``object_name``."""
        ...

    async def put_object_from_local_file(
        self,
        *,
        object_name: str,
        local_path: Path | str,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Upload a file from disk under ``object_name``."""
        ...

    async def download_storage_path_to_local_file(self, *, storage_path: str, dest_path: Path) -> None:
        """Download ``storage_path`` (``bucket/object``) to ``dest_path``."""
        ...

    def object_name_from_reference(self, object_reference: str) -> str:
        """Normalise a storage reference to a key within this store's bucket."""
        ...

    async def check_readiness(self) -> None:
        """Verify the store is reachable and the configured bucket is usable."""
        ...


@dataclass
class _MemoryObject:
    data: bytes
    content_type: str
    metadata: Mapping[str, str]


class InMemoryObjectStore:
    """In-memory object store for unit tests and wiring checks.

    Not for production use. Presigned URLs are synthetic ``memory://`` links.
    """

    def __init__(
        self,
        *,
        bucket_name: str = "test-bucket",
        allowed_prefixes: frozenset[str] | None = None,
        auto_create_bucket: bool = True,
    ) -> None:
        self.bucket_name = bucket_name
        self.allowed_prefixes = allowed_prefixes or frozenset({"uploads"})
        self._auto_create_bucket = auto_create_bucket
        self._bucket_ready = auto_create_bucket
        self._objects: dict[str, _MemoryObject] = {}

    def close(self) -> None:
        self._objects.clear()

    def object_name_from_reference(self, object_reference: str) -> str:
        return resolve_object_name(
            object_reference,
            bucket_name=self.bucket_name,
            allowed_prefixes=self.allowed_prefixes,
        )

    async def upload_file(
        self,
        *,
        file_obj: BinaryIO,
        filename: str,
        prefix: str = "uploads",
        max_bytes: int | None = None,
    ) -> StoredObject:
        self._ensure_bucket()
        safe_filename = safe_basename(filename)
        normalised_prefix = normalise_prefix(prefix)
        if normalised_prefix.split("/", maxsplit=1)[0] not in self.allowed_prefixes:
            raise ValueError(f"unsupported prefix {prefix!r}; accepted: {sorted(self.allowed_prefixes)}")
        object_name = f"{normalised_prefix}/{uuid.uuid4()}_{safe_filename}"
        file_obj.seek(0, 2)
        size_bytes = file_obj.tell()
        if max_bytes is not None and size_bytes > max_bytes:
            raise ObjectTooLargeError(f"object size {size_bytes} exceeds max {max_bytes}")
        file_obj.seek(0)
        data = file_obj.read()
        content_type = content_type_for(safe_filename)
        self._objects[object_name] = _MemoryObject(data=data, content_type=content_type, metadata={})
        return StoredObject(
            bucket_name=self.bucket_name,
            object_name=object_name,
            storage_path=f"{self.bucket_name}/{object_name}",
            content_type=content_type,
            size_bytes=len(data),
        )

    async def stat_object(self, object_name: str) -> None:
        key = self.object_name_from_reference(object_name)
        if key not in self._objects:
            raise ObjectNotFoundError(f"object {key!r} was not found")

    async def presigned_get_url(
        self,
        *,
        object_name: str,
        expires_seconds: int,
        disposition: ContentDisposition = "auto",
        download_filename: str | None = None,
    ) -> PresignedObjectUrl:
        _ = disposition, download_filename
        key = self.object_name_from_reference(object_name)
        if key not in self._objects:
            raise ObjectNotFoundError(f"object {key!r} was not found")
        return PresignedObjectUrl(
            url=f"memory://{self.bucket_name}/{key}",
            bucket_name=self.bucket_name,
            object_name=key,
            expires_seconds=expires_seconds,
        )

    async def put_object_from_local_file(
        self,
        *,
        object_name: str,
        local_path: Path | str,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self._ensure_bucket()
        parts = object_name.split("/")
        if any(p in ("", ".", "..") or "\\" in p for p in parts):
            raise ValueError("object_name contains an unsafe path segment")
        if parts[0] not in self.allowed_prefixes:
            raise ValueError(
                f"unsupported object prefix {parts[0]!r}; accepted: {sorted(self.allowed_prefixes)}"
            )
        data = Path(local_path).read_bytes()
        self._objects[object_name] = _MemoryObject(
            data=data,
            content_type=content_type,
            metadata=dict(metadata or {}),
        )
        return StoredObject(
            bucket_name=self.bucket_name,
            object_name=object_name,
            storage_path=f"{self.bucket_name}/{object_name}",
            content_type=content_type,
            size_bytes=len(data),
        )

    async def download_storage_path_to_local_file(self, *, storage_path: str, dest_path: Path) -> None:
        key = self.object_name_from_reference(storage_path)
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"object {key!r} was not found")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(obj.data)

    async def check_readiness(self) -> None:
        if self._bucket_ready or self._auto_create_bucket:
            return
        raise ObjectStorageError(
            f"object storage bucket {self.bucket_name!r} does not exist and "
            "auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
        )

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if not self._auto_create_bucket:
            raise ObjectStorageError(
                f"object storage bucket {self.bucket_name!r} does not exist and "
                "auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
            )
        self._bucket_ready = True

    def put_bytes_for_tests(
        self,
        object_name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Seed an object without going through upload (test helper)."""
        self._bucket_ready = True
        self._objects[object_name] = _MemoryObject(data=data, content_type=content_type, metadata={})

    def open_bytes_for_tests(self, object_name: str) -> BinaryIO:
        """Return a readable buffer of a stored object (test helper)."""
        obj = self._objects[object_name]
        return io.BytesIO(obj.data)


__all__ = [
    "ContentDisposition",
    "InMemoryObjectStore",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStorageError",
    "ObjectTooLargeError",
    "PresignedObjectUrl",
    "StoredObject",
    "content_type_for",
    "looks_like_object_storage_storage_path",
    "normalise_prefix",
    "resolve_object_name",
    "safe_basename",
]
