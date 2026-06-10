"""Base settings — extended by each service with service-specific fields."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseAppSettings(BaseSettings):
    """Minimal settings shared by all services.

    Each service defines its own Settings subclass:
        class Settings(BaseAppSettings): ...
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "microcoaching"
    app_env: str = "development"
    log_level: str = "INFO"
    log_json: bool = True
    log_service_name: str | None = None
