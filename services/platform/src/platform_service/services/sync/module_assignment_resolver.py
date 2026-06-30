"""Resolve which module IDs are assigned to a user for device sync."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.services.user_service import get_all_users


async def resolve_assigned_module_ids(
    session: AsyncSession,
    *,
    user_id: int,
    organization_ids: list[int] | None = None,
) -> set[UUID]:
    """Return module IDs assigned to ``user_id`` via group, geo, or individual rules."""
    users_by_id = {u["id"]: u for u in get_all_users()}
    caller_user = users_by_id.get(user_id)

    filters = []

    # 1. Tenant/Group level
    if organization_ids:
        filters.append(
            (CHWModuleAssignment.assignment_type == "group")
            & CHWModuleAssignment.tenant_id.in_(organization_ids)
        )

    # 2. Geographical (Upazila) level
    if caller_user and caller_user.get("upazila"):
        filters.append(
            (CHWModuleAssignment.assignment_type == "geographical")
            & (CHWModuleAssignment.upazila == caller_user["upazila"])
        )

    # 3. Individual / PO+SK level
    if caller_user:
        role = caller_user.get("role")
        parent_id = caller_user.get("parent_id")

        if role == "SK":
            # SK gets direct individual assignment or parent PO's po_sk assignment
            filters.append(
                (CHWModuleAssignment.user_id == user_id)
                & (CHWModuleAssignment.assignment_type == "individual")
            )
            if parent_id is not None:
                filters.append(
                    (CHWModuleAssignment.user_id == parent_id)
                    & (CHWModuleAssignment.assignment_type == "po_sk")
                )
        elif role == "PO":
            # PO gets direct individual or po_sk assignment
            filters.append(
                (CHWModuleAssignment.user_id == user_id)
                & CHWModuleAssignment.assignment_type.in_(["individual", "po_sk"])
            )
        else:
            # AM or other role gets direct assignment
            filters.append(CHWModuleAssignment.user_id == user_id)
    else:
        # Fallback if user_id is not in hardcoded users
        filters.append(CHWModuleAssignment.user_id == user_id)

    if not filters:
        return set()

    stmt = select(CHWModuleAssignment.module_id).where(or_(*filters))
    return set((await session.execute(stmt)).scalars().all())
