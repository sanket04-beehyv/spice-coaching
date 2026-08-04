"""Resolve which video source documents are assigned to a user for device sync."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_video_assignment import CHWVideoAssignment
from platform_service.services.user_service import get_all_users


def _assignment_filters(
    *,
    user_id: int,
    organization_ids: list[int] | None,
) -> list:
    users_by_id = {u["id"]: u for u in get_all_users()}
    caller_user = users_by_id.get(user_id)

    filters = []

    # 1. Tenant/Group level
    if organization_ids:
        filters.append(
            (CHWVideoAssignment.assignment_type == "group")
            & CHWVideoAssignment.tenant_id.in_(organization_ids)
        )

    # 2. Geographical (Upazila) level
    if caller_user and caller_user.get("upazila"):
        filters.append(
            (CHWVideoAssignment.assignment_type == "geographical")
            & (CHWVideoAssignment.upazila == caller_user["upazila"])
        )

    # 3. Individual / PO+SK level
    if caller_user:
        role = caller_user.get("role")
        parent_id = caller_user.get("parent_id")

        if role == "SK":
            filters.append(
                (CHWVideoAssignment.user_id == user_id) & (CHWVideoAssignment.assignment_type == "individual")
            )
            if parent_id is not None:
                filters.append(
                    (CHWVideoAssignment.user_id == parent_id)
                    & (CHWVideoAssignment.assignment_type == "po_sk")
                )
        elif role == "PO":
            filters.append(
                (CHWVideoAssignment.user_id == user_id)
                & CHWVideoAssignment.assignment_type.in_(["individual", "po_sk"])
            )
        else:
            filters.append(CHWVideoAssignment.user_id == user_id)
    else:
        filters.append(CHWVideoAssignment.user_id == user_id)

    return filters


async def resolve_assigned_videos(
    session: AsyncSession,
    *,
    user_id: int,
    organization_ids: list[int] | None = None,
) -> dict[UUID, datetime]:
    """Return source_document_id -> latest assigned_at for videos assigned to ``user_id``."""
    filters = _assignment_filters(user_id=user_id, organization_ids=organization_ids)
    if not filters:
        return {}

    stmt = (
        select(
            CHWVideoAssignment.source_document_id,
            func.max(CHWVideoAssignment.assigned_at),
        )
        .where(or_(*filters))
        .group_by(CHWVideoAssignment.source_document_id)
    )
    res = await session.execute(stmt)
    return {row[0]: row[1] for row in res.all()}
