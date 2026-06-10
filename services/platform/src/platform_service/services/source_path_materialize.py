"""Resolve ingest ``source_path`` values to a local file for extractors.

``POST /admin/ingest`` stores ``original_storage_path`` as
``{minio_bucket_name}/{object_key}``. Legacy rows use an absolute filesystem
path. Stage A extractors expect a readable local path, so the pipeline
materialises object-storage references into a temp file under
``{upload_dir}/ingest_work/`` and deletes it after the run completes.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from platform_service.config import get_settings
from platform_service.services.object_storage import looks_like_object_storage_storage_path


async def materialize_local_source_file(source_ref: str | Path) -> tuple[Path, Path | None]:
    """Return ``(local_path, temp_path_to_delete_or_none)``."""
    ref_str = str(source_ref).strip()
    candidate = Path(ref_str)
    if candidate.is_absolute() and candidate.is_file():
        return candidate, None

    settings = get_settings()
    if not looks_like_object_storage_storage_path(ref_str, bucket_name=settings.minio_bucket_name):
        return Path(ref_str), None

    from platform_service.deps import get_object_storage_client

    client = get_object_storage_client()
    work_dir = Path(settings.upload_dir) / "ingest_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(ref_str).suffix or ".bin"
    tmp = work_dir / f".pipeline-{uuid.uuid4()}{suffix}"
    try:
        await client.download_storage_path_to_local_file(storage_path=ref_str, dest_path=tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, tmp
