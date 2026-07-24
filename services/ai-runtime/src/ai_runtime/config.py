"""AI runtime settings — extends mc_foundation BaseAppSettings."""

from __future__ import annotations

from functools import lru_cache
from typing import Self

from mc_contracts.internal_ai import AiProvider
from mc_foundation.config import BaseAppSettings
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

_PLACEHOLDER_VALUES = frozenset({"replace-with-strong-internal-token", "dev-internal-token", "default=key"})
_DEPLOYED_ENVS = frozenset({"production", "staging"})


class Settings(BaseAppSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ai-runtime"

    # ── Auth ─────────────────────────────────────────────────────────────────
    # Simple shared-secret token for internal service-to-service auth.
    # Platform sends X-Internal-Token header; we verify it here.
    internal_token: SecretStr = SecretStr("")

    # ── AI Provider ───────────────────────────────────────────────────────────
    # Single source of truth for generate / embed / transcribe routing.
    # Accepts ai_provider or legacy ai_cloud_provider env alias.
    ai_provider: AiProvider = Field(
        default="google",
        validation_alias=AliasChoices("ai_provider", "ai_cloud_provider"),
    )

    # Google Gemini — supports both Vertex AI and the Developer API.
    # google_use_vertex=true ⇒ ADC via GOOGLE_APPLICATION_CREDENTIALS env var
    # plus google_cloud_project / google_cloud_location.
    # Otherwise the SDK falls back to api-key auth via google_api_key.
    google_use_vertex: bool = False
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    google_api_key: SecretStr = SecretStr("")
    # Base64-encoded service-account JSON. When set, forces Vertex mode and
    # builds credentials in-process (no GOOGLE_APPLICATION_CREDENTIALS file
    # needed). Standard pattern for passing GCP credentials in container
    # deployments where mounting a JSON file isn't ergonomic.
    google_service_account_base64: SecretStr | None = None
    google_embedding_model: str = "gemini-embedding-001"
    google_embedding_dimension: int = 768

    google_transcription_model: str = "gemini-2.5-flash"

    # Target pgvector corpus dimension; the canonical truncation point lives in
    # ``services/embedding_vector.align_embedding_dimension`` and runs once per
    # ``PromptExecutor.embed`` call. Platform-side helpers assert against this
    # value instead of re-truncating.
    embedding_dimension: int = 768

    # ── Generation defaults ───────────────────────────────────────────────────
    # Used as fallback when a GenerationType is missing from GENERATION_PROFILES
    # (tests assert the map is complete). Per-type budgets live in
    # ``ai_runtime.generation_profiles``.
    default_inference_model: str = "gemini-2.5-flash"
    default_max_tokens: int = 8192
    default_temperature: float = 0.2
    json_parse_retries: int = 1
    # Log every successful LLM response body at INFO when true. Parse failures
    # are always logged at WARNING regardless of this flag.
    log_llm_responses: bool = True
    # Truncate logged LLM bodies beyond this length (full text still returned in
    # InferenceResponse.raw_text).
    log_llm_response_max_chars: int = 20000
    # Per-provider SDK HTTP timeout (seconds). Keep below platform httpx timeout
    # so ai-runtime fails fast instead of holding the upstream connection.
    provider_timeout_seconds: float = 590.0

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Self:
        if self.app_env not in _DEPLOYED_ENVS:
            return self
        errors: list[str] = []
        internal_token = self.internal_token.get_secret_value().strip()
        if not internal_token:
            errors.append("INTERNAL_TOKEN must be configured in production")
        if len(internal_token) < 32 or internal_token in _PLACEHOLDER_VALUES:
            errors.append("INTERNAL_TOKEN must be at least 32 chars and not a placeholder")
        google_api_key = self.google_api_key.get_secret_value().strip()
        if (
            self.ai_provider == "google"
            and not self.google_use_vertex
            and (not google_api_key or google_api_key in _PLACEHOLDER_VALUES)
        ):
            errors.append("GOOGLE_API_KEY or Vertex credentials are required in production")
        if errors:
            raise ValueError("; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
