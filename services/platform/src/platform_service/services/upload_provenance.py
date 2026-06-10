"""Upload-time provenance for objects stored in MinIO.

``build_upload_metadata`` stamps sha256 and original filename on MinIO user
metadata at ``put_object`` time. ``record_file_upload`` persists the same
fields to the ``file_upload`` audit table when ingest uploads complete.
"""

from platform_service.db.models.file_upload import FileUpload
from platform_service.db.repositories.file_upload_repository import FileUploadRepository

META_SHA256 = "content-sha256"
META_FILENAME = "original-filename"


def build_upload_metadata(*, content_sha256: str, original_filename: str) -> dict[str, str]:
    """MinIO user metadata stamped at ``put_object`` time."""
    return {
        META_SHA256: content_sha256,
        META_FILENAME: original_filename,
    }


def parse_upload_metadata(raw: dict[str, str] | None) -> tuple[str | None, str | None]:
    """Normalise S3/MinIO stat metadata keys to (content_sha256, original_filename)."""
    if not raw:
        return None, None
    normalised: dict[str, str] = {}
    for key, value in raw.items():
        k = key.lower()
        if k.startswith("x-amz-meta-"):
            k = k.removeprefix("x-amz-meta-")
        normalised[k] = value
    return normalised.get(META_SHA256), normalised.get(META_FILENAME)


async def record_file_upload(
    *,
    file_upload_repo: FileUploadRepository,
    bucket_name: str,
    object_key: str,
    storage_path: str,
    original_filename: str,
    content_sha256: str,
    content_type: str | None,
    size_bytes: int,
    uploaded_by: str | None,
) -> FileUpload:
    return await file_upload_repo.upsert(
        bucket_name=bucket_name,
        object_key=object_key,
        storage_path=storage_path,
        original_filename=original_filename,
        content_sha256=content_sha256,
        content_type=content_type,
        size_bytes=size_bytes,
        uploaded_by=uploaded_by,
    )
