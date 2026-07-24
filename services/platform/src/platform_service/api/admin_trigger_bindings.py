"""Admin trigger binding endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from mc_contracts.admin_modules import (
    CreateBindingRequest,
    TriggerBindingPayload,
    UpdateBindingRequest,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.trigger_definition import ModuleTriggerBinding
from platform_service.db.repositories.module_family_repository import ModuleFamilyRepository
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.deps import get_db

router = APIRouter(prefix="/admin", tags=["admin-trigger-bindings"])


def _binding_to_payload(binding: ModuleTriggerBinding) -> TriggerBindingPayload:
    return TriggerBindingPayload(
        id=binding.id,
        trigger_definition_id=binding.trigger_definition_id,
        module_id=binding.module_id,
        relationship=binding.relationship,
        priority_weight=binding.priority_weight,
        notes=binding.notes,
    )


@router.get("/trigger-bindings/by-module/{module_id}", response_model=list[TriggerBindingPayload])
async def list_bindings_for_module(
    module_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> list[TriggerBindingPayload]:
    repo = TriggerRepository(session)
    bindings = await repo.list_bindings_for_module(module_id)
    return [_binding_to_payload(b) for b in bindings]


@router.post("/trigger-bindings", response_model=TriggerBindingPayload)
async def create_binding(
    body: CreateBindingRequest,
    session: AsyncSession = Depends(get_db),
) -> TriggerBindingPayload:
    repo = TriggerRepository(session)
    family_repo = ModuleFamilyRepository(session)
    module = await session.get(Module, body.module_id)
    if module is None or not await family_repo.is_assignable(module.module_family_id):
        raise HTTPException(
            status_code=409,
            detail="cannot bind trigger to a deactivated or unpublished module",
        )
    binding = await repo.bind_module_to_trigger(
        trigger_definition_id=body.trigger_definition_id,
        module_id=body.module_id,
        relationship=body.relationship,
        priority_weight=body.priority_weight,
        notes=body.notes,
    )
    await session.commit()
    return _binding_to_payload(binding)


@router.put("/trigger-bindings/{binding_id}", response_model=TriggerBindingPayload)
async def update_binding(
    binding_id: UUID,
    body: UpdateBindingRequest,
    session: AsyncSession = Depends(get_db),
) -> TriggerBindingPayload:
    binding = await session.get(ModuleTriggerBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="trigger binding not found")
    if body.relationship is not None:
        if body.relationship not in ("primary", "secondary"):
            raise HTTPException(
                status_code=400,
                detail="relationship must be 'primary' or 'secondary'",
            )
        binding.relationship = body.relationship
    if body.priority_weight is not None:
        binding.priority_weight = body.priority_weight
    if body.notes is not None:
        binding.notes = body.notes
    await session.flush()
    await session.commit()
    return _binding_to_payload(binding)


@router.delete("/trigger-bindings/{binding_id}")
async def delete_binding(
    binding_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, str | bool]:
    binding = await session.get(ModuleTriggerBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="trigger binding not found")
    await session.delete(binding)
    await session.commit()
    return {"id": str(binding_id), "deleted": True}
