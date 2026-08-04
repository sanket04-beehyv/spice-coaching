"""boto3-backed ObjectStore for MinIO and AWS S3."""

from __future__ import annotations

import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal
from urllib.parse import quote, urlparse

import anyio
import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mc_foundation.objectstore import (
    ContentDisposition,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectTooLargeError,
    PresignedObjectUrl,
    StoredObject,
    content_type_for,
    normalise_prefix,
    resolve_object_name,
    safe_basename,
)

ObjectStorageBackend = Literal["minio", "s3"]
PresignMode = Literal["direct", "proxy"]


def _endpoint_url(endpoint: str | None, *, secure: bool) -> str | None:
    if not endpoint or not endpoint.strip():
        return None
    raw = endpoint.strip()
    parsed = urlparse(raw if "://" in raw else f"{'https' if secure else 'http'}://{raw}")
    if parsed.path not in ("", "/"):
        raise ValueError("object storage endpoint must not include a path")
    if not parsed.netloc:
        raise ValueError("object storage endpoint is invalid")
    scheme = "https" if secure else "http"
    return f"{scheme}://{parsed.netloc}"


def _content_disposition_for(object_name: str, *, inline: bool, download_filename: str | None = None) -> str:
    filename = (download_filename or PurePosixPath(object_name).name).strip()
    if not filename:
        filename = PurePosixPath(object_name).name
    disp = "inline" if inline else "attachment"
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        return f"{disp}; filename*=UTF-8''{quote(filename)}"
    return f'{disp}; filename="{filename}"'


def _inline_default_for_content_type(content_type: str) -> bool:
    if content_type == "application/pdf":
        return True
    return content_type.startswith(("image/", "video/", "audio/"))


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in {
        "404",
        "NoSuchBucket",
        "NoSuchKey",
        "NotFound",
        "404 Not Found",
    }


def _build_s3_client(
    *,
    endpoint_url: str | None,
    region: str,
    access_key: str,
    secret_key: str,
    addressing_style: Literal["auto", "path"] = "auto",
) -> BaseClient:
    # AWS S3 rejects SigV2; always force AWS4-HMAC-SHA256 for uploads and presigns.
    s3_config: dict[str, str] = {}
    if addressing_style == "path":
        s3_config["addressing_style"] = "path"
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": region or None,
        "config": Config(signature_version="s3v4", s3=s3_config),
    }
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client(**kwargs)


class S3ObjectStore:
    """Async wrapper around a sync boto3 S3 client (MinIO or AWS)."""

    def __init__(
        self,
        *,
        bucket_name: str,
        region: str = "us-east-1",
        endpoint: str | None = None,
        presigned_endpoint: str | None = None,
        access_key: str = "",
        secret_key: str = "",
        secure: bool = True,
        backend: ObjectStorageBackend = "s3",
        presign_mode: PresignMode = "direct",
        allowed_prefixes: frozenset[str] | None = None,
        auto_create_bucket: bool = False,
    ) -> None:
        self.bucket_name = bucket_name
        self.allowed_prefixes = allowed_prefixes or frozenset({"uploads"})
        self._backend = backend
        self._presign_mode = presign_mode
        # Auto-create is only allowed for local MinIO; never against AWS.
        self._auto_create_bucket = auto_create_bucket if backend == "minio" else False
        self._bucket_ready = False

        data_endpoint = _endpoint_url(endpoint, secure=secure)
        if backend == "minio" and not data_endpoint:
            raise ValueError("OBJECT_STORAGE_ENDPOINT is required when OBJECT_STORAGE_BACKEND=minio")
        if backend == "minio" and (not access_key or not secret_key):
            raise ValueError(
                "OBJECT_STORAGE_ACCESS_KEY and OBJECT_STORAGE_SECRET_KEY are required "
                "when OBJECT_STORAGE_BACKEND=minio"
            )

        addressing: Literal["auto", "path"] = "path" if backend == "minio" else "auto"
        self._client = _build_s3_client(
            endpoint_url=data_endpoint,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            addressing_style=addressing,
        )

        if presign_mode == "proxy":
            proxy_endpoint = _endpoint_url(presigned_endpoint or endpoint, secure=secure)
            self._presign_client = _build_s3_client(
                endpoint_url=proxy_endpoint,
                region=region,
                access_key=access_key,
                secret_key=secret_key,
                addressing_style=addressing,
            )
        else:
            self._presign_client = self._client

    def close(self) -> None:
        """Drop client references so connection pools can be reclaimed."""
        self._presign_client = None  # type: ignore[assignment]
        self._client = None  # type: ignore[assignment]

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
        return await anyio.to_thread.run_sync(
            self._upload_file_sync,
            file_obj,
            filename,
            prefix,
            max_bytes,
        )

    async def stat_object(self, object_name: str) -> None:
        await anyio.to_thread.run_sync(self._stat_object_sync, object_name)

    async def presigned_get_url(
        self,
        *,
        object_name: str,
        expires_seconds: int,
        disposition: ContentDisposition = "auto",
        download_filename: str | None = None,
    ) -> PresignedObjectUrl:
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
        return await anyio.to_thread.run_sync(
            self._put_object_from_local_file_sync,
            object_name,
            str(local_path),
            content_type,
            metadata,
        )

    async def download_storage_path_to_local_file(self, *, storage_path: str, dest_path: Path) -> None:
        object_name = self.object_name_from_reference(storage_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        def _download() -> None:
            try:
                self._client.download_file(self.bucket_name, object_name, str(dest_path))
            except ClientError as exc:
                if _is_not_found(exc):
                    raise ObjectNotFoundError(f"object {object_name!r} was not found") from exc
                raise ObjectStorageError(f"failed to download object {object_name!r}") from exc
            except BotoCoreError as exc:
                raise ObjectStorageError(f"failed to download object {object_name!r}") from exc

        await anyio.to_thread.run_sync(_download)

    async def check_readiness(self) -> None:
        await anyio.to_thread.run_sync(self._check_readiness_sync)

    def _stat_object_sync(self, object_name: str) -> None:
        normalised = self.object_name_from_reference(object_name)
        try:
            self._client.head_object(Bucket=self.bucket_name, Key=normalised)
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(f"object {normalised!r} was not found") from exc
            raise ObjectStorageError(f"failed to stat object {normalised!r}") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError(f"failed to stat object {normalised!r}") from exc

    def _upload_file_sync(
        self,
        file_obj: BinaryIO,
        filename: str,
        prefix: str,
        max_bytes: int | None,
    ) -> StoredObject:
        safe_filename = safe_basename(filename)
        normalised_prefix = normalise_prefix(prefix)
        if normalised_prefix.split("/", maxsplit=1)[0] not in self.allowed_prefixes:
            raise ValueError(f"unsupported prefix {prefix!r}; accepted: {sorted(self.allowed_prefixes)}")
        object_name = f"{normalised_prefix}/{uuid.uuid4()}_{safe_filename}"
        resolved_content_type = content_type_for(safe_filename)

        try:
            self._ensure_bucket()
            file_obj.seek(0, 2)
            size_bytes = file_obj.tell()
            if max_bytes is not None and size_bytes > max_bytes:
                raise ObjectTooLargeError(f"object size {size_bytes} exceeds max {max_bytes}")
            file_obj.seek(0)
            self._client.put_object(
                Bucket=self.bucket_name,
                Key=object_name,
                Body=file_obj,
                ContentType=resolved_content_type,
                ContentLength=size_bytes,
            )
        except ObjectTooLargeError:
            raise
        except ClientError as exc:
            raise ObjectStorageError(f"failed to upload object {object_name!r}") from exc
        except BotoCoreError as exc:
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
            extra: dict[str, Any] = {"ContentType": content_type}
            if metadata:
                # boto3 expects Metadata values as strings without the x-amz-meta- prefix.
                extra["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
            self._client.upload_file(
                local_path,
                self.bucket_name,
                object_name,
                ExtraArgs=extra,
            )
        except ClientError as exc:
            raise ObjectStorageError(f"failed to upload object {object_name!r}") from exc
        except BotoCoreError as exc:
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
        normalised = self.object_name_from_reference(object_name)
        try:
            self._client.head_object(Bucket=self.bucket_name, Key=normalised)
            content_type = content_type_for(normalised)
            if disposition == "auto":
                inline = _inline_default_for_content_type(content_type)
            else:
                inline = disposition == "inline"
            disp_header = _content_disposition_for(
                normalised,
                inline=inline,
                download_filename=download_filename,
            )
            url = self._presign_client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": normalised,
                    "ResponseContentType": content_type,
                    "ResponseContentDisposition": disp_header,
                },
                ExpiresIn=expires_seconds,
            )
            return PresignedObjectUrl(
                url=url,
                bucket_name=self.bucket_name,
                object_name=normalised,
                expires_seconds=expires_seconds,
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(f"object {normalised!r} was not found") from exc
            raise ObjectStorageError(f"failed to generate presigned URL for {normalised!r}") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError(f"failed to generate presigned URL for {normalised!r}") from exc

    def _check_readiness_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket_name)
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchBucket", "NotFound", "404 Not Found"}:
                raise ObjectStorageError(
                    f"object storage readiness check failed for bucket {self.bucket_name!r}"
                ) from exc
        except BotoCoreError as exc:
            raise ObjectStorageError(
                f"object storage readiness check failed for bucket {self.bucket_name!r}"
            ) from exc

        if not self._auto_create_bucket:
            raise ObjectStorageError(
                f"object storage bucket {self.bucket_name!r} does not exist and "
                "auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
            )

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        try:
            self._client.head_bucket(Bucket=self.bucket_name)
            self._bucket_ready = True
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"404", "NoSuchBucket", "NotFound", "404 Not Found"}:
                raise ObjectStorageError(f"failed to check bucket {self.bucket_name!r}") from exc
        except BotoCoreError as exc:
            raise ObjectStorageError(f"failed to check bucket {self.bucket_name!r}") from exc

        if not self._auto_create_bucket:
            raise ObjectStorageError(
                f"object storage bucket {self.bucket_name!r} does not exist and "
                "auto_create_bucket is false; create the bucket in infra or enable auto-create in dev."
            )
        try:
            create_kwargs: dict[str, Any] = {"Bucket": self.bucket_name}
            # us-east-1 is the only region that must omit LocationConstraint.
            region = getattr(self._client.meta, "region_name", None) or "us-east-1"
            if region != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            self._client.create_bucket(**create_kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise ObjectStorageError(f"failed to create bucket {self.bucket_name!r}") from exc
        self._bucket_ready = True
