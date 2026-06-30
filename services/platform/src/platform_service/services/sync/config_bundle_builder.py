"""Build config sync bundles for device sync."""

from __future__ import annotations

from datetime import UTC, datetime

from mc_contracts.sync import ConfigSyncBundle
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.repositories.config_threshold_repository import ConfigThresholdRepository


class ConfigBundleBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(self) -> ConfigSyncBundle:
        rows = await ConfigThresholdRepository(self._session).list_all()
        thresholds = {row.key: row.value_json for row in rows}
        settings = get_settings()
        return ConfigSyncBundle(
            thresholds=thresholds,
            locales=settings.deployment_locale_config,
            server_time_utc=datetime.now(UTC).isoformat(),
        )
