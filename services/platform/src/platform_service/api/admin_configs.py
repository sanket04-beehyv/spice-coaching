from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mc_contracts.admin_configs import (
    ConfigThresholdResponse,
    ConfigThresholdUpdateRequest,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.config_threshold_repository import ConfigThresholdRepository
from platform_service.deps import get_db

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])


@router.get("/configs", response_model=list[ConfigThresholdResponse])
async def list_configs(
    session: AsyncSession = Depends(get_db),
) -> list[ConfigThresholdResponse]:
    """Retrieve all configuration thresholds."""
    repo = ConfigThresholdRepository(session)
    configs = await repo.list_all()
    return [ConfigThresholdResponse.model_validate(c) for c in configs]


@router.get("/configs/{key}", response_model=ConfigThresholdResponse)
async def get_config(
    key: str,
    session: AsyncSession = Depends(get_db),
) -> ConfigThresholdResponse:
    """Retrieve a single configuration threshold by key."""
    repo = ConfigThresholdRepository(session)
    config = await repo.get_by_key(key)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Config threshold with key '{key}' not found.",
        )
    return ConfigThresholdResponse.model_validate(config)


@router.put("/configs/{key}", response_model=ConfigThresholdResponse)
async def update_config(
    key: str,
    body: ConfigThresholdUpdateRequest,
    session: AsyncSession = Depends(get_db),
) -> ConfigThresholdResponse:
    """Update an existing configuration threshold."""
    repo = ConfigThresholdRepository(session)
    config = await repo.get_by_key(key)
    if config is None:
        raise HTTPException(
            status_code=404,
            detail=f"Config threshold with key '{key}' not found.",
        )

    updated = await repo.update_config(
        config=config,
        value_json=body.value_json,
        title=body.title,
        description=body.description,
    )
    await session.commit()
    await session.refresh(updated)
    return ConfigThresholdResponse.model_validate(updated)
