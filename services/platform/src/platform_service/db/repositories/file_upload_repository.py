"""Persistence for ``file_upload`` provenance rows."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.file_upload import FileUpload


class FileUploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        bucket_name: str,
        object_key: str,
        storage_path: str,
        original_filename: str,
        content_sha256: str,
        content_type: str | None,
        size_bytes: int,
        uploaded_by: str | None,
    ) -> FileUpload:
        stmt = (
            insert(FileUpload)
            .values(
                bucket_name=bucket_name,
                object_key=object_key,
                storage_path=storage_path,
                original_filename=original_filename,
                content_sha256=content_sha256,
                content_type=content_type,
                size_bytes=size_bytes,
                uploaded_by=uploaded_by,
            )
            .on_conflict_do_update(
                index_elements=["bucket_name", "object_key"],
                set_={
                    "storage_path": storage_path,
                    "original_filename": original_filename,
                    "content_sha256": content_sha256,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                    "uploaded_by": uploaded_by,
                },
            )
            .returning(FileUpload)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one()
        await self._session.flush()
        return row

    async def find_latest_by_content_sha256(
        self,
        *,
        bucket_name: str,
        content_sha256: str,
    ) -> FileUpload | None:
        result = await self._session.execute(
            select(FileUpload)
            .where(
                FileUpload.bucket_name == bucket_name,
                FileUpload.content_sha256 == content_sha256,
            )
            .order_by(FileUpload.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
