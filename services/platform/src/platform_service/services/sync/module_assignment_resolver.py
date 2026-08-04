"""Resolve which module IDs are assigned to a user for device sync."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.module_availability import is_training_module_family
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

    return filters


async def resolve_assigned_module_ids(
    session: AsyncSession,
    *,
    user_id: int,
    organization_ids: list[int] | None = None,
) -> set[UUID]:
    """Return module IDs assigned to ``user_id`` via group, geo, or individual rules."""
    filters = _assignment_filters(user_id=user_id, organization_ids=organization_ids)
    if not filters:
        return set()

    stmt = (
        select(CHWModuleAssignment.module_id)
        .join(Module, CHWModuleAssignment.module_id == Module.id)
        .join(ModuleFamily, Module.module_family_id == ModuleFamily.id)
        .where(or_(*filters), is_training_module_family())
    )
    return set((await session.execute(stmt)).scalars().all())


async def resolve_assigned_modules(
    session: AsyncSession,
    *,
    user_id: int,
    organization_ids: list[int] | None = None,
) -> dict[UUID, datetime]:
    """Return module_id -> latest assigned_at for modules assigned to ``user_id``."""
    filters = _assignment_filters(user_id=user_id, organization_ids=organization_ids)
    if not filters:
        return {}

    stmt = (
        select(
            CHWModuleAssignment.module_id,
            func.max(CHWModuleAssignment.assigned_at),
        )
        .where(or_(*filters))
        .group_by(CHWModuleAssignment.module_id)
    )
    res = await session.execute(stmt)
    return {row[0]: row[1] for row in res.all()}
