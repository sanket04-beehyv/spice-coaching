"""Object storage helpers for platform-owned uploads."""

from __future__ import annotations

import mimetypes
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal
from urllib.parse import quote, urlparse

import anyio
from minio import Minio
from minio.error import S3Error

from platform_service.config import Settings, get_settings

ContentDisposition = Literal["auto", "inline", "attachment"]


class ObjectStorageError(RuntimeError):
    """Raised when MinIO cannot complete an object storage operation."""


class ObjectNotFoundError(ObjectStorageError):
    """Raised when the requested object does not exist."""


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


class ObjectTooLargeError(ObjectStorageError):
    """Raised when an upload exceeds the configured size limit."""


def looks_like_object_storage_storage_path(ref: str, *, bucket_name: str) -> bool:
    """True when ``ref`` is stored as ``{bucket_name}/{object_key}`` in MinIO."""
    return str(ref).strip().startswith(f"{bucket_name}/")


def _normalise_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme and parsed.netloc:
        if parsed.path not in ("", "/"):
            raise ValueError("MinIO endpoint must not include a path")
        return parsed.netloc
    return endpoint


def safe_basename(filename: str) -> str:
    """Last path segment of ``filename``, safe for object keys."""
    basename = filename.replace("\\", "/").split("/")[-1].strip()
    if not basename:
        raise ValueError("filename is required")
    return basename


def _normalise_prefix(prefix: str) -> str:
    clean = prefix.strip().strip("/")
    if not clean:
        return "uploads"

    parts = clean.split("/")
    if any(part in ("", ".", "..") or "\\" in part for part in parts):
        raise ValueError("prefix contains an unsafe path segment")
    return "/".join(parts)


def _content_disposition_for(object_name: str, *, inline: bool, download_filename: str | None = None) -> str:
    """Build a Content-Disposition header for the presign response.

    ``download_filename`` overrides the filename rendered to the user
    (otherwise the UUID-prefixed object key basename leaks through —
    a CHW would see ``uuid_manual.pdf`` instead of ``manual.pdf``).
    """
    filename = (download_filename or PurePosixPath(object_name).name).strip()
    if not filename:
        filename = PurePosixPath(object_name).name
    disp = "inline" if inline else "attachment"
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        return f"{disp}; filename*=UTF-8''{quote(filename)}"
    return f'{disp}; filename="{filename}"'


def _content_type_for(object_name: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(object_name)[0] or fallback


def _inline_default_for_content_type(content_type: str) -> bool:
    """Prefer inline preview in the browser for PDFs, images, and AV sources.

    The CHW field-assistant UX (``/coaching/rag-query`` source attribution)
    wants "tap to play at timecode" for video/audio sources, not "tap to
    download". Inline disposition makes the browser play the file in-place
    so the timecode in the presigned URL lands on a media element.
    """
    if content_type == "application/pdf":
        return True
    return content_type.startswith(("image/", "video/", "audio/"))


class ObjectStorageClient:
    """Small async wrapper around the sync MinIO SDK."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        secure: bool = False,
        region: str | None = None,
        presigned_endpoint: str | None = None,
        allowed_prefixes: frozenset[str] | None = None,
        auto_create_bucket: bool = True,
    ) -> None:
        self.bucket_name = bucket_name
        self.allowed_prefixes = allowed_prefixes or frozenset({"uploads"})
        self._auto_create_bucket = auto_create_bucket
        self._bucket_ready = False
        self._client = Minio(
            _normalise_endpoint(endpoint),
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
        )
        self._presigned_client = (
            Minio(
                _normalise_endpoint(presigned_endpoint),
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
                region=region,
            )
            if presigned_endpoint
            else self._client
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ObjectStorageClient:
        settings = settings or get_settings()
        return cls(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            bucket_name=settings.minio_bucket_name,
            secure=settings.minio_secure,
            region=settings.minio_region,
            presigned_endpoint=settings.minio_presigned_endpoint,
            allowed_prefixes=settings.admin_file_allowed_prefix_set,
            auto_create_bucket=settings.minio_auto_create_bucket,
        )

    def close(self) -> None:
        """Drop MinIO client references so urllib3 pools can be reclaimed."""
        self._presigned_client = None  # type: ignore[assignment]
        self._client = None  # type: ignore[assignment]

    async def upload_file(
        self,
        *,
        file_obj: BinaryIO,
        filename: str,
        prefix: str = "uploads",
        max_bytes: int | None = None,
    ) -> StoredObject:
        return await anyio.to_thread.run_sync(
            self._upload_file_sync,
            file_obj,
            filename,
            prefix,
            max_bytes,
        )

    async def stat_object(self, object_name: str) -> None:
        """Verify an object exists in the configured bucket."""
        await anyio.to_thread.run_sync(self._stat_object_sync, object_name)

    def _stat_object_sync(self, object_name: str) -> None:
        normalised_object_name = self.object_name_from_reference(object_name)
        try:
            self._client.stat_object(self.bucket_name, normalised_object_name)
        except S3Error as exc:
            if exc.code in {"NoSuchBucket", "NoSuchKey"}:
                raise ObjectNotFoundError(f"object {normalised_object_name!r} was not found") from exc
            raise ObjectStorageError(f"failed to stat object {normalised_object_name!r}") from exc

    async def presigned_get_url(
        self,
        *,
        object_name: str,
        expires_seconds: int,
        disposition: ContentDisposition = "auto",
        download_filename: str | None = None,
    ) -> PresignedObjectUrl:
        """Build a presigned GET URL.

        ``download_filename`` is rendered in the response
        ``Content-Disposition`` so the browser saves/labels the file with
        the caller's preferred name (the original upload filename) rather
        than the UUID-prefixed object key.
        """
        return await anyio.to_thread.run_sync(
            self._presigned_get_url_sync,
            object_name,
            expires_seconds,
            disposition,
            download_filename,
        )

    async def put_object_from_local_file(
        self,
        *,
        object_name: str,
        local_path: Path | str,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Upload a file from disk into this client's bucket under ``object_name``.

        ``metadata`` is written as MinIO user metadata (prefixed
        ``x-amz-meta-`` on the wire), e.g. content sha256 and original filename.
        """
        return await anyio.to_thread.run_sync(
            self._put_object_from_local_file_sync,
            object_name,
            str(local_path),
            content_type,
            metadata,
        )

    async def download_storage_path_to_local_file(self, *, storage_path: str, dest_path: Path) -> None:
        """Download ``storage_path`` (``bucket/object``) to ``dest_path``."""
        object_name = self.object_name_from_reference(storage_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def _download() -> None:
            try:
                self._client.fget_object(self.bucket_name, object_name, str(dest_path))
            except S3Error as exc:
                if exc.code in {"NoSuchBucket", "NoSuchKey"}:
                    raise ObjectNotFoundError(f"object {object_name!r} was not found") from exc
                raise ObjectStorageError(f"failed to download object {object_name!r}") from exc

        await anyio.to_thread.run_sync(_download)

    def object_name_from_reference(self, object_reference: str) -> str:
        """Normalise ``bucket/key`` or ``key`` to an object key within this client's bucket.

        If the reference starts with a bucket name segment:
        - It **must** match ``self.bucket_name``; otherwise ``ValueError`` is raised
          (avoids silently reading the wrong bucket after a misconfigured rename).
        - If it matches, the bucket prefix is stripped and the remainder is validated.
        If there is no bucket prefix, the first path segment must be an allowed prefix.
        """
        clean = object_reference.strip().lstrip("/")
        if not clean:
            raise ValueError("object_name is required")

        first, sep, rest = clean.partition("/")
        if sep:
            if first == self.bucket_name:
                clean = rest
            elif first not in self.allowed_prefixes:
                raise ValueError(
                    f"object storage reference starts with {first!r}, which is neither "
                    f"this service bucket ({self.bucket_name!r}) nor an allowed object prefix "
                    f"({sorted(self.allowed_prefixes)})"
                )

        if not clean:
            raise ValueError("object_name is required")
        parts = clean.split("/")
        if any(part in ("", ".", "..") or "\\" in part for part in parts):
            raise ValueError("object reference contains an unsafe path segment")
        object_prefix = clean.split("/", maxsplit=1)[0]
        if object_prefix not in self.allowed_prefixes:
            raise ValueError(
                f"unsupported object prefix {object_prefix!r}; accepted: {sorted(self.allowed_prefixes)}"
            )
        return clean

    def _upload_file_sync(
        self,
        file_obj: BinaryIO,
        filename: str,
        prefix: str,
        max_bytes: int | None,
    ) -> StoredObject:
        safe_filename = safe_basename(filename)
        normalised_prefix = _normalise_prefix(prefix)
        if normalised_prefix.split("/", maxsplit=1)[0] not in self.allowed_prefixes:
            raise ValueError(f"unsupported prefix {prefix!r}; accepted: {sorted(self.allowed_prefixes)}")
        object_name = f"{normalised_prefix}/{uuid.uuid4()}_{safe_filename}"
        resolved_content_type = _content_type_for(safe_filename)

        try:
            self._ensure_bucket()
            file_obj.seek(0, 2)
            size_bytes = file_obj.tell()
            if max_bytes is not None and size_bytes > max_bytes:
                raise ObjectTooLargeError(f"object size {size_bytes} exceeds max {max_bytes}")
            file_obj.seek(0)
            self._client.put_object(
                self.bucket_name,
                object_name,
                file_obj,
                length=size_bytes,
                content_type=resolved_content_type,
            )
        except ObjectTooLargeError:
            raise
        except S3Error as exc:
            raise ObjectStorageError(f"failed to upload object {object_name!r}") from exc

        return StoredObject(
            bucket_name=self.bucket_name,
            object_name=object_name,
            storage_path=f"{self.bucket_name}/{object_name}",
            content_type=resolved_content_type,
            size_bytes=size_bytes,
        )

    def _put_object_from_local_file_sync(
        self,
        object_name: str,
        local_path: str,
        content_type: str,
        metadata: dict[str, str] | None,
    ) -> StoredObject:
        parts = object_name.split("/")
        if any(p in ("", ".", "..") or "\\" in p for p in parts):
            raise ValueError("object_name contains an unsafe path segment")
        if parts[0] not in self.allowed_prefixes:
            raise ValueError(
                f"unsupported object prefix {parts[0]!r}; accepted: {sorted(self.allowed_prefixes)}"
            )
        try:
            self._ensure_bucket()
            self._client.fput_object(
                self.bucket_name,
                object_name,
                local_path,
                content_type=content_type,
                metadata=metadata,
            )
        except S3Error as exc:
            raise ObjectStorageError(f"failed to upload object {object_name!r}") from exc

        size_bytes = os.path.getsize(local_path)
        return StoredObject(
            bucket_name=self.bucket_name,
            object_name=object_name,
            storage_path=f"{self.bucket_name}/{object_name}",
            content_type=content_type,
            size_bytes=size_bytes,
        )

    def _presigned_get_url_sync(
        self,
        object_name: str,
        expires_seconds: int,
        disposition: ContentDisposition,
        download_filename: str | None,
    ) -> PresignedObjectUrl:
        normalised_object_name = self.object_name_from_reference(object_name)
        try:
            self._client.stat_object(self.bucket_name, normalised_object_name)
            content_type = _content_type_for(normalised_object_name)
            if disposition == "auto":
                inline = _inline_default_for_content_type(content_type)
            else:
                inline = disposition == "inline"
            disp_header = _content_disposition_for(
                normalised_object_name,
                inline=inline,
                download_filename=download_filename,
            )
            url = self._presigned_client.get_presigned_url(
                "GET",
                self.bucket_name,
                normalised_object_name,
                expires=timedelta(seconds=expires_seconds),
                response_headers={
                    "response-content-type": content_type,
                    "response-content-disposition": disp_header,
                },
            )
            return PresignedObjectUrl(
                url=url,
                bucket_name=self.bucket_name,
                object_name=normalised_object_name,
                expires_seconds=expires_seconds,
            )
        except S3Error as exc:
            if exc.code in {"NoSuchBucket", "NoSuchKey"}:
                raise ObjectNotFoundError(f"object {normalised_object_name!r} was not found") from exc
            raise ObjectStorageError(
                f"failed to generate presigned URL for {normalised_object_name!r}"
            ) from exc

    async def check_readiness(self) -> None:
        """Verify MinIO is reachable and the configured bucket is usable."""
        await anyio.to_thread.run_sync(self._check_readiness_sync)

    def _check_readiness_sync(self) -> None:
        try:
            exists = self._client.bucket_exists(self.bucket_name)
        except S3Error as exc:
            raise ObjectStorageError(f"MinIO readiness check failed for bucket {self.bucket_name!r}") from exc
        if exists:
            return
        if not self._auto_create_bucket:
            raise ObjectStorageError(
                f"MinIO bucket {self.bucket_name!r} does not exist and "
                "minio_auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
            )

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        exists = self._client.bucket_exists(self.bucket_name)
        if exists:
            self._bucket_ready = True
            return
        if not self._auto_create_bucket:
            raise ObjectStorageError(
                f"MinIO bucket {self.bucket_name!r} does not exist and "
                "minio_auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
            )
        try:
            self._client.make_bucket(self.bucket_name)
        except S3Error as exc:
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
        self._bucket_ready = True
