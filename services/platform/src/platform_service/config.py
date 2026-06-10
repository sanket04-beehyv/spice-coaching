"""Platform service settings.

Per `docs/ARCHITECTURE_RESET.md`. Service code reads from `Settings`;
nothing reads `os.environ` directly. All thresholds and feature flags live
here so they are env-overridable and unit-testable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Self
from uuid import UUID

from mc_foundation.config import BaseAppSettings
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from platform_service.tenant_mapping import parse_spice_tenant_id_map

_INSECURE_DB_PASSWORDS = frozenset({"dpkgyl", "postgres"})
_PLACEHOLDER_VALUES = frozenset(
    {
        "replace-with-strong-db-password",
        "replace-with-strong-internal-token",
        "replace-with-minio-access-key",
        "replace-with-minio-secret-key",
    }
)

AiCloudProvider = Literal["google", "openai"]


class Settings(BaseAppSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "platform-api"
    # Public URL prefix for all platform-api routes (e.g. /medtronics-api).
    api_root_path: str = "/medtronics-api/"

    # ── Database ──────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/microcoaching"
    # Sourced from env in production. Default None so a missing env var
    # surfaces clearly at first DB connection rather than silently using
    # someone's stale local password.
    database_password: SecretStr | None = None
    database_pool_size: int = 5
    database_max_overflow: int = 10
    database_pool_recycle: int = 3600
    database_pool_pre_ping: bool = True
    redis_url: str = "redis://localhost:6379/0"
    # ClickHouse retry-buffer drain (Celery beat).
    telemetry_buffer_drain_batch_size: int = 100
    telemetry_buffer_drain_interval_seconds: int = 60

    # Comma-separated CORS origins.
    cors_allow_origins: str = "http://localhost:5173,http://localhost:3000"

    # ── ClickHouse ────────────────────────────────────────────
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "default"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""

    # ── AI Runtime (internal call) ────────────────────────────
    ai_runtime_base_url: str = "http://localhost:8001"
    ai_runtime_timeout_seconds: float = 600.0
    ai_runtime_transcribe_timeout_seconds: float = 600.0
    # Service token sent in X-Internal-Token header.
    ai_runtime_token: SecretStr = SecretStr("")

    # ── SPICE auth-service (token validation) ───────────────────
    spice_auth_enabled: bool = True
    # Auth-service root as seen by platform (direct or reverse-proxy).
    # e.g. http://authservice:8089 or http://gateway/auth-service
    spice_auth_base_url: str = "http://localhost:8089"
    spice_auth_timeout_seconds: float = 5.0
    # Fallback ``client`` header when the caller omits it (SPICE mobile = mob).
    spice_auth_default_client: str = "mob"
    # Comma-separated path suffixes exempt from auth (under api_root_path).
    spice_auth_exempt_paths: str = "health,ready"
    # Comma-separated relative path prefixes (under api_root_path) per authorization plane.
    spice_admin_path_prefixes: str = "admin,dashboard"
    spice_device_path_prefixes: str = "telemetry,sync,morning,coaching"
    # JSON ``{"1": "<uuid>", ...}`` or comma-separated ``1=<uuid>,2=<uuid>``.
    spice_tenant_id_map: str = ""

    # ── Rate limiting (Redis sliding window per client IP) ─────
    rate_limit_enabled: bool = True
    rate_limit_telemetry_per_minute: int = 120
    rate_limit_rag_per_minute: int = 30
    rate_limit_admin_ingest_per_minute: int = 10

    # ── Embedding ─────────────────────────────────────────────
    embedding_dimension: int = 768
    top_k: int = 5
    retrieval_require_validated: bool = False

    # ── Safety ────────────────────────────────────────────────
    spice_referral_destinations: str = (
        "Upazila Health Complex (UHC),"
        "Community Clinic,"
        "District Hospital,"
        "Union Health Centre,"
        "Mother and Child Welfare Centre (MCWC),"
        "Emergency Obstetric Care (EmOC),"
        "Nutrition Rehabilitation Unit (NRU),"
        "Upazila Health and Family Planning Officer (UHFPO),"
        "BRAC Health Programme Clinic"
    )

    @property
    def spice_referral_set(self) -> frozenset[str]:
        return frozenset(d.strip() for d in self.spice_referral_destinations.split(",") if d.strip())

    @property
    def api_root_path_normalized(self) -> str:
        """Leading slash, no trailing slash (e.g. ``/medtronics-api``).

        Special-case: ``""`` and ``"/"`` mean "no base path" and normalize to ``""``.
        """
        raw = (self.api_root_path or "").strip()
        if raw in {"", "/"}:
            return ""
        clean = raw.strip("/")
        if not clean:
            # Covers cases like "///" or whitespace-only.
            return ""
        return f"/{clean}"

    @property
    def spice_auth_authenticate_url(self) -> str:
        return f"{self.spice_auth_base_url.rstrip('/')}/authenticate"

    @property
    def spice_auth_exempt_path_set(self) -> frozenset[str]:
        root = self.api_root_path_normalized
        paths: set[str] = set()
        for suffix in self.spice_auth_exempt_paths.split(","):
            clean = suffix.strip().strip("/")
            if clean:
                if root:
                    paths.add(f"{root}/{clean}")
                else:
                    paths.add(f"/{clean}")
        return frozenset(paths)

    @property
    def spice_admin_path_prefix_set(self) -> frozenset[str]:
        return self._spice_path_prefix_set(self.spice_admin_path_prefixes)

    @property
    def spice_device_path_prefix_set(self) -> frozenset[str]:
        return self._spice_path_prefix_set(self.spice_device_path_prefixes)

    def _spice_path_prefix_set(self, raw: str) -> frozenset[str]:
        prefixes: set[str] = set()
        for item in raw.split(","):
            clean = item.strip().strip("/").lower()
            if clean:
                prefixes.add(clean)
        return frozenset(prefixes)

    @property
    def spice_tenant_uuid_by_id(self) -> dict[int, UUID]:
        try:
            return parse_spice_tenant_id_map(self.spice_tenant_id_map)
        except ValueError as exc:
            raise ValueError(f"invalid SPICE_TENANT_ID_MAP: {exc}") from exc

    # ── Stage 1 — quality heuristic thresholds ──────────────────
    extraction_quality_text_empty_min_chars: int = 50
    # Native-script encoding integrity: pages with < this native-script
    # codepoint frequency AND > non_ascii_byte_max non-ASCII byte rate are
    # treated as legacy ANSI/Bijoy encoding and routed to vision fallback.
    # Applies to any language with a registered script in
    # `quality_heuristic._NATIVE_SCRIPT_RANGES` (bn / hi / mr / ta / te /
    # bn_en_mixed). Generalised from Bangla-only after the Hindi-Bijoy
    # ASHA Induction PDF bypassed the bn-only check and the identifier
    # silently dropped TB & FP modules.
    extraction_quality_native_codepoint_min: float = 0.40
    extraction_quality_non_ascii_byte_max: float = 0.25

    # ── Stage 1 — calibration ───────────────────────────────────
    extraction_calibration_sample_size: int = 10
    extraction_calibration_force_vision_threshold: float = 0.80
    extraction_calibration_skip_vision_threshold: float = 0.20

    # ── Stage 1 — vision recovery pass ──────────────────────────
    # After the main per-page loop, any page that raised during vision
    # extraction is retried in a recovery pass. The recovery pass waits
    # `stage_a_vision_recovery_initial_delay_s` first (lets the Vertex
    # per-minute quota window clear) and then retries each failed page
    # serially up to `stage_a_vision_recovery_max_retries` times. This
    # complements the in-call retry inside `prompt_executor` — the in-call
    # retry handles short bursts; this recovery pass handles sustained
    # quota windows that span the full main-loop duration.
    stage_a_vision_recovery_initial_delay_s: float = 60.0
    stage_a_vision_recovery_max_retries: int = 3
    # Tolerance: pages that remain vision_failed AFTER the recovery pass.
    # 0 = strict (any unrecovered page fails Stage 1). For the SK pilot
    # (small corpus, every chapter matters) keep at 0; raise for noisier
    # production volumes if individual page failures become acceptable.
    stage_a_vision_failed_tolerance: int = 0

    # ── Stage 2 — module identification ─────────────────────────
    # Advisory thresholds for the insufficient-source heuristic. The
    # heuristic flags a candidate when source provenance is below either
    # bound; per the architecture reset these flags are written onto the
    # candidate's quality_flags_jsonb but DO NOT reject the candidate.
    stage_c_insufficient_source_min_tokens: int = 50
    stage_c_insufficient_source_min_headings: int = 1
    # Default `module.domain` value when Stage 2's candidate doesn't carry
    # a domain. Override via DEFAULT_MODULE_DOMAIN env var.
    default_module_domain: str = "rmnch"
    # Output-token budget for the identification LLM call. Bumped from 32K
    # to 60K to give the LLM room to emit 25-30 candidates with full
    # provenance for one chunk's worth of corpus content. Override via
    # STAGE_C_MAX_OUTPUT_TOKENS env var.
    stage_c_max_output_tokens: int = 12_000
    # ── Stage 2 — token-budget chunker ──────────────────────────
    # Target tokens per chunk. Empirical data from Run 6 on the SK manual:
    # 35K finished in ~4 min, 40K and 68K both timed out at the 15-min
    # AI_RUNTIME_TIMEOUT_SECONDS ceiling. 25K is comfortably below the
    # 35K proven-safe ceiling and yields ~10 chunks for the SK manual,
    # each completing deterministically.
    stage_c_chunk_target_tokens: int = 25_000
    # Window (as a fraction of target) within which the chunker will look
    # for an outline section boundary to break at. 0.10 = ±10%. If no
    # boundary is found within the window, the chunker breaks at the
    # current page.
    stage_c_chunk_window_pct: float = 0.10
    # Trigram-Jaccard similarity threshold for cross-chunk near-duplicate
    # flagging. Candidates from different chunks with normalised-title
    # similarity ≥ this are tagged `cross_chunk_near_duplicate` for
    # reviewer attention. Same-title candidates are merged deterministically
    # (no LLM); near-similar titles are flagged but kept separate so the
    # reviewer makes the merge call. Lowered from 0.80 → 0.70 after the
    # Induction-Hindi run produced a substring-overlap STI pair scoring
    # 0.757 — just under the prior threshold. 0.70 still high enough to
    # avoid false positives on genuinely different topics.
    stage_c_cross_chunk_similarity_threshold: float = 0.70
    # How many per-chunk identification calls run in parallel. 2 is
    # conservative against Vertex per-project quota.
    stage_c_section_concurrency: int = 2

    # ── Stage 2-draft — bilingual card drafting ─────────────────
    # Cardinality bounds. Quiz bounds also apply to the post-publish quiz
    # generation worker.
    quiz_min_questions: int = 3
    quiz_max_questions: int = 10
    card_min_count: int = 3
    card_max_count: int = 7

    # ── Post-publish — behavioural gap classification ───────────
    post_publish_gap_classification_enabled: bool = True
    gap_classification_max_associations: int = 5

    # ── Stage 2-draft — optional published-module merge ─────────
    # When enabled, Stage D may merge newly drafted cards into a similar
    # active (non-retired) published module.
    stage_d_published_merge_enabled: bool = True
    # Pre-filter existing modules by title similarity before sending to the LLM.
    stage_d_published_merge_prefilter_limit: int = 25
    # Output-token budget for the merge LLM call.
    stage_d_published_merge_max_output_tokens: int = 4_000
    # Deterministic content gate: fraction of existing cards that must match
    # the new card set (by text similarity or shared source_block_id).
    stage_d_published_merge_min_existing_card_match_ratio: float = 0.0
    # Per existing card, best new-card trigram similarity must exceed this.
    stage_d_published_merge_card_similarity_threshold: float = 0.0
    # Whole-module fingerprint similarity floor (both card sets concatenated).
    stage_d_published_merge_module_similarity_threshold: float = 0.0

    # ── Quiz pass / retrigger ───────────────────────────────────
    quiz_pass_threshold_default: float = 0.70
    quiz_periodic_refresh_days: int = 90
    quiz_failure_escalation_count: int = 3
    quiz_failure_escalation_window_days: int = 30

    # ── Trigger sensitivity ─────────────────────────────────────
    # Default gap-trigger occurrence threshold; per-module override via
    # trigger_definition.predicate_jsonb.
    gap_trigger_default_occurrences: int = 2
    gap_trigger_default_window_days: int = 14

    # ── Staging retention ───────────────────────────────────────
    staging_retention_days: int = 30

    upload_dir: str = "/tmp/microcoaching/uploads"
    # Max seconds the ingest batch worker waits for a thumbnail Celery task before Stage A.
    ingest_thumbnail_wait_seconds: int = 300

    # ── Object storage ──────────────────────────────────────────
    minio_endpoint: str = "localhost:9100"
    minio_presigned_endpoint: str | None = None
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_bucket_name: str = "medtronics-storage"
    # When false, missing buckets raise at first upload instead of make_bucket (production default).
    minio_auto_create_bucket: bool = True
    minio_secure: bool = False
    minio_region: str = "us-east-1"
    admin_file_allowed_prefixes: str = "uploads,source-documents,media,ingest,module-thumbnails"
    admin_file_max_upload_bytes: int = 100 * 1024 * 1024
    admin_file_presigned_max_seconds: int = 24 * 60 * 60

    # ── Module editor attachments (module_json inline refs) ───
    module_attachment_allowed_prefix: str = "media"
    module_attachment_max_per_module: int = 20
    module_attachment_max_per_card: int = 10

    @property
    def admin_file_allowed_prefix_set(self) -> frozenset[str]:
        return frozenset(
            prefix.strip().strip("/")
            for prefix in self.admin_file_allowed_prefixes.split(",")
            if prefix.strip().strip("/")
        )

    # ── AI Runtime model selection ──────────────────────────────
    # Selects which model *names* platform sends in InferenceRequest.model_policy.
    # Must match ai-runtime's AI_CLOUD_PROVIDER / ai_provider (same env var in compose).
    ai_cloud_provider: AiCloudProvider = "openai"

    google_inference_model: str = "gemini-2.5-flash"
    google_identification_model: str = "gemini-2.5-pro"
    google_embedding_model: str = "text-embedding-005"
    google_vision_model: str = "gemini-2.5-flash"

    openai_inference_model: str = "gpt-4o-mini"
    openai_identification_model: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_vision_model: str = "gpt-4o-mini"

    @property
    def text_model(self) -> str:
        return (
            self.openai_inference_model if self.ai_cloud_provider == "openai" else self.google_inference_model
        )

    @property
    def identification_model(self) -> str:
        return (
            self.openai_identification_model
            if self.ai_cloud_provider == "openai"
            else self.google_identification_model
        )

    @property
    def embedding_model(self) -> str:
        return (
            self.openai_embedding_model if self.ai_cloud_provider == "openai" else self.google_embedding_model
        )

    @property
    def vision_model(self) -> str:
        return self.openai_vision_model if self.ai_cloud_provider == "openai" else self.google_vision_model

    @property
    def cors_allow_origins_list(self) -> list[str]:
        raw = (self.cors_allow_origins or "").strip()
        if raw == "*":
            return ["*"]
        return [part.strip() for part in raw.split(",") if part.strip()]

    @field_validator("cors_allow_origins")
    @classmethod
    def _strip_cors_origins(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Self:
        if self.app_env != "production":
            return self
        errors: list[str] = []
        database_password = (self.database_password.get_secret_value().strip() if self.database_password else "")
        if not database_password or database_password in _INSECURE_DB_PASSWORDS:
            errors.append("DATABASE_PASSWORD must be set to a non-default value in production")
        if database_password in _PLACEHOLDER_VALUES:
            errors.append("DATABASE_PASSWORD must not use a placeholder value in production")
        token_value = self.ai_runtime_token.get_secret_value().strip()
        if not token_value:
            errors.append("AI_RUNTIME_TOKEN must be configured in production")
        if len(token_value) < 32 or token_value in _PLACEHOLDER_VALUES:
            errors.append("AI_RUNTIME_TOKEN must be at least 32 chars and not a placeholder")
        minio_access_key = self.minio_access_key.get_secret_value().strip()
        if not minio_access_key:
            errors.append("MINIO_ACCESS_KEY must be configured in production")
        if minio_access_key in _PLACEHOLDER_VALUES:
            errors.append("MINIO_ACCESS_KEY must not use a placeholder value")
        minio_secret_key = self.minio_secret_key.get_secret_value().strip()
        if not minio_secret_key:
            errors.append("MINIO_SECRET_KEY must be configured in production")
        if minio_secret_key in _PLACEHOLDER_VALUES:
            errors.append("MINIO_SECRET_KEY must not use a placeholder value")
        if not self.spice_auth_enabled:
            errors.append("SPICE_AUTH_ENABLED must be true in production")
        if not self.spice_tenant_id_map.strip():
            errors.append("SPICE_TENANT_ID_MAP must be configured in production")
        if "*" in self.cors_allow_origins_list:
            errors.append("CORS_ALLOW_ORIGINS must not include '*' in production")
        if errors:
            raise ValueError("; ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
