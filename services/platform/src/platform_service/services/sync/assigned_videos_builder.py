"""Build paginated assigned-videos payloads for device sync."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import AssignedVideoPayload, AssignedVideosBundle, VideoProgressPayload
from mc_foundation.objectstore import ObjectStore
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.db.repositories.video_progress_repository import VideoProgressRepository
from platform_service.services.source_thumbnail_service import presign_thumbnail
from platform_service.services.sync.video_assignment_resolver import resolve_assigned_videos

logger = logging.getLogger(__name__)


class AssignedVideosBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self,
        *,
        user_id: int,
        storage: ObjectStore,
        organization_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
        settings: Settings | None = None,
    ) -> AssignedVideosBundle:
        settings = settings or get_settings()
        assigned = await resolve_assigned_videos(
            self._session,
            user_id=user_id,
            organization_ids=organization_ids,
        )
        if not assigned:
            logger.info(
                "assigned_videos user_id=%s total_videos=0 limit=%s offset=%s",
                user_id,
                limit,
                offset,
            )
            return AssignedVideosBundle(
                videos=[],
                total_videos=0,
                total_pages=0,
                limit=limit,
                offset=offset,
                server_time_utc=datetime.now(UTC).isoformat(),
            )

        video_ids = list(assigned.keys())
        duration_by_id = await self._duration_ms_by_document(video_ids)
        progress_repo = VideoProgressRepository(self._session)
        progress_by_id = await progress_repo.get_by_chw_and_videos(user_id, video_ids)

        stmt = select(SourceDocument).where(
            SourceDocument.id.in_(video_ids),
            SourceDocument.source_type == "video",
        )
        docs = list((await self._session.execute(stmt)).scalars().all())
        docs_by_id = {doc.id: doc for doc in docs}

        rows: list[tuple[SourceDocument, datetime]] = []
        for video_id, assigned_at in assigned.items():
            doc = docs_by_id.get(video_id)
            if doc is None:
                continue
            rows.append((doc, assigned_at))

        rows.sort(key=lambda item: item[1], reverse=True)
        total_videos = len(rows)
        total_pages = (total_videos + limit - 1) // limit if total_videos > 0 else 0
        page = rows[offset : offset + limit]

        videos: list[AssignedVideoPayload] = []
        for doc, assigned_at in page:
            thumb = await presign_thumbnail(
                storage,
                thumbnail_storage_path=doc.thumbnail_storage_path,
                settings=settings,
            )
            progress_row = progress_by_id.get(doc.id)
            video_progress = (
                VideoProgressPayload(
                    last_position_ms=progress_row.last_position_ms,
                    percent_watched=progress_row.percent_watched,
                    completed=progress_row.completed,
                    last_watched_at=progress_row.last_watched_at,
                )
                if progress_row
                else None
            )
            videos.append(
                AssignedVideoPayload(
                    video_id=doc.id,
                    title=doc.title,
                    description=doc.description,
                    thumbnail_storage_path=doc.thumbnail_storage_path,
                    thumbnail_presigned_url=thumb[0] if thumb else None,
                    thumbnail_presigned_expires_seconds=thumb[1] if thumb else None,
                    duration_ms=duration_by_id.get(doc.id),
                    assigned_at=assigned_at,
                    video_progress=video_progress,
                )
            )

        logger.info(
            "assigned_videos user_id=%s total_videos=%s returned=%s limit=%s offset=%s",
            user_id,
            total_videos,
            len(videos),
            limit,
            offset,
        )
        return AssignedVideosBundle(
            videos=videos,
            total_videos=total_videos,
            total_pages=total_pages,
            limit=limit,
            offset=offset,
            server_time_utc=datetime.now(UTC).isoformat(),
        )

    async def _duration_ms_by_document(self, video_ids: list[UUID]) -> dict[UUID, int]:
        stmt = (
            select(
                SourcePage.source_document_id,
                func.max(SourcePage.end_ms),
            )
            .where(
                SourcePage.source_document_id.in_(video_ids),
                SourcePage.end_ms.is_not(None),
            )
            .group_by(SourcePage.source_document_id)
        )
        result = await self._session.execute(stmt)
        return {row[0]: int(row[1]) for row in result.all() if row[1] is not None}
