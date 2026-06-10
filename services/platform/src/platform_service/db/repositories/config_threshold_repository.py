from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.config_threshold import ConfigThreshold


def _coerce_json_to_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


class ConfigThresholdRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_value(self, key: str) -> Any | None:
        stmt = select(ConfigThreshold.value_json).where(ConfigThreshold.key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_int(self, key: str, default: int) -> int:
        value = await self.get_value(key)
        return _coerce_json_to_int(value, default)

    async def get_int_for_keys(self, defaults: dict[str, int]) -> dict[str, int]:
        """Return a copy of `defaults` with any matching `config_threshold` rows overriding values."""
        if not defaults:
            return {}
        keys = tuple(defaults.keys())
        stmt = select(ConfigThreshold.key, ConfigThreshold.value_json).where(ConfigThreshold.key.in_(keys))
        rows = (await self.session.execute(stmt)).all()
        out = dict(defaults)
        for row_key, value_json in rows:
            if row_key in out:
                out[row_key] = _coerce_json_to_int(value_json, out[row_key])
        return out

    async def list_all(self) -> list[ConfigThreshold]:
        stmt = select(ConfigThreshold)
        return list((await self.session.execute(stmt)).scalars().all())
