"""Configuration tests, aligned to the post-architecture-reset settings.

The original W-0 file asserted defaults for many fields that the
architecture reset removed (see config.py P3 cleanup): feature flags
(`generate_embeddings_on_publish`), snippet thresholds, distractor
critique knobs, reviewer-queue settings, outline thresholds. Those
fields are gone and the corresponding tests would simply
`AttributeError`.

This rewrite covers the fields that survived:
- Database / Redis / ClickHouse base settings
- Embedding dimension (still load-bearing for pgvector column type)
- Stage-1 calibration thresholds (extraction_*)
- Stage-2 + Stage-2-draft cardinality bounds + insufficient-source heuristic
- Quiz pass / retrigger / escalation
- Trigger sensitivity defaults
- Model selection (post-P3, defaults to gemini-2.5-{flash,pro})
- Sanity invariants (skip < force, min < max)
- env override + invalid value handling
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from platform_service.config import Settings, get_settings
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Each test starts with a fresh `get_settings()` cache, clean env vars
    for the fields we assert defaults on, and **`.env` loading disabled**
    so the developer's local `.env` doesn't shadow the code defaults.

    Tests that want a specific env override call `monkeypatch.setenv`
    after this fixture runs.
    """
    get_settings.cache_clear()
    # Override the class's SettingsConfigDict to NOT load any .env file.
    monkeypatch.setattr(
        Settings,
        "model_config",
        SettingsConfigDict(env_file=None, env_file_encoding="utf-8", extra="ignore"),
    )
    # Belt-and-braces: also clear common env-var overrides set by the
    # smoke-loop diagnostic or the dev's shell.
    for var in (
        "EXTRACTION_CALIBRATION_FORCE_VISION_THRESHOLD",
        "EXTRACTION_CALIBRATION_SKIP_VISION_THRESHOLD",
        "EXTRACTION_CALIBRATION_SAMPLE_SIZE",
        "STAGE_C_INSUFFICIENT_SOURCE_MIN_TOKENS",
        "QUIZ_PASS_THRESHOLD_DEFAULT",
        "APP_ENV",
        "OBJECT_STORAGE_BACKEND",
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_PRESIGNED_ENDPOINT",
        "OBJECT_STORAGE_PRESIGN_MODE",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
        "OBJECT_STORAGE_BUCKET_NAME",
        "OBJECT_STORAGE_SECURE",
        "OBJECT_STORAGE_REGION",
        "OBJECT_STORAGE_AUTO_CREATE_BUCKET",
        "ADMIN_FILE_ALLOWED_PREFIXES",
        "ADMIN_FILE_UPLOAD_PREFIX",
        "ADMIN_FILE_PRESIGNED_MAX_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    get_settings.cache_clear()


# ─── Defaults ───────────────────────────────────────────────────────────────


def test_app_name_default() -> None:
    assert Settings().app_name == "platform-api"


def test_api_root_path_default() -> None:
    assert Settings().api_root_path == "/medtronics-api/"
    assert Settings().api_root_path_normalized == "/medtronics-api"


def test_api_root_path_normalized_empty_means_no_base_path() -> None:
    assert Settings(api_root_path="").api_root_path_normalized == ""
    assert Settings(api_root_path="/").api_root_path_normalized == ""
    assert Settings(api_root_path="  /  ").api_root_path_normalized == ""


def test_api_root_path_normalized_strips_slashes() -> None:
    assert Settings(api_root_path="medtronics-api").api_root_path_normalized == "/medtronics-api"
    assert Settings(api_root_path="/medtronics-api/").api_root_path_normalized == "/medtronics-api"


def test_api_path_with_default_root() -> None:
    settings = Settings()
    assert settings.api_path("/admin/ingest/batches/abc") == "/medtronics-api/admin/ingest/batches/abc"


def test_api_path_without_leading_slash() -> None:
    settings = Settings(api_root_path="/medtronics-api/")
    assert settings.api_path("admin/ingest") == "/medtronics-api/admin/ingest"


def test_api_path_with_empty_root() -> None:
    settings = Settings(api_root_path="")
    assert settings.api_path("/admin/ingest/batches/abc") == "/admin/ingest/batches/abc"


def test_spice_auth_exempt_path_set_with_empty_root() -> None:
    s = Settings(api_root_path="")
    assert "/ready" in s.spice_auth_exempt_path_set
    assert "/health" not in s.spice_auth_exempt_path_set


def test_embedding_dimension_default() -> None:
    """pgvector column types depend on this; changing it requires a migration."""
    assert Settings().embedding_dimension == 768


def test_vector_store_backend_default() -> None:
    assert Settings().vector_store_backend == "pgvector"


def test_top_k_default() -> None:
    assert Settings().top_k == 5


def test_retrieval_require_validated_default_false() -> None:
    assert Settings().retrieval_require_validated is False


# ─── Stage 1 — calibration thresholds ──────────────────────────────────────


def test_extraction_thresholds_defaults() -> None:
    s = Settings()
    assert s.extraction_quality_text_empty_min_chars == 50
    assert s.extraction_quality_native_codepoint_min == 0.40
    assert s.extraction_quality_non_ascii_byte_max == 0.25
    assert s.extraction_calibration_sample_size == 10
    assert s.extraction_calibration_force_vision_threshold == 0.80
    assert s.extraction_calibration_skip_vision_threshold == 0.20


def test_calibration_skip_below_force() -> None:
    """Per-page zone exists when skip < force; otherwise the per_page
    branch is unreachable."""
    s = Settings()
    assert s.extraction_calibration_skip_vision_threshold < s.extraction_calibration_force_vision_threshold


# ─── Stage 2 — module identification ───────────────────────────────────────


def test_stage_c_chunk_target_default() -> None:
    # Stage 2 chunker target. 25K tokens per chunk; for the SK manual
    # (~250K tokens) this yields ~10 chunks comfortably below Vertex's
    # 15-min timeout ceiling.
    assert Settings().stage_c_chunk_target_tokens == 25_000
    assert 0.05 <= Settings().stage_c_chunk_window_pct <= 0.25


def test_insufficient_source_thresholds_present() -> None:
    """Heuristic is advisory now (P1 fix), but the thresholds still drive
    the `quality_flags_jsonb` content."""
    s = Settings()
    assert s.stage_c_insufficient_source_min_tokens == 50
    assert s.stage_c_insufficient_source_min_headings == 1


def test_cross_chunk_similarity_threshold_in_range() -> None:
    """Trigram similarity threshold for cross-chunk near-duplicate flagging."""
    s = Settings()
    assert 0.0 < s.stage_c_cross_chunk_similarity_threshold < 1.0


# ─── Stage 2-draft — cardinality bounds ────────────────────────────────────


def test_quiz_and_card_bounds() -> None:
    s = Settings()
    assert s.quiz_min_questions == 3
    assert s.quiz_max_questions == 10
    assert s.card_min_count == 3
    assert s.card_max_count == 10


def test_quiz_and_card_bounds_form_valid_intervals() -> None:
    """min < max for both, otherwise validator hits an unsatisfiable range."""
    s = Settings()
    assert s.quiz_min_questions < s.quiz_max_questions
    assert s.card_min_count < s.card_max_count


# ─── Quiz / retrigger / escalation ─────────────────────────────────────────


def test_quiz_retrigger_defaults() -> None:
    s = Settings()
    assert s.quiz_pass_threshold_default == 0.70
    assert s.quiz_periodic_refresh_days == 90
    assert s.quiz_failure_escalation_count == 3
    assert s.quiz_failure_escalation_window_days == 30


# ─── Trigger sensitivity ───────────────────────────────────────────────────


def test_trigger_defaults() -> None:
    s = Settings()
    assert s.gap_trigger_default_occurrences == 2
    assert s.gap_trigger_default_window_days == 14


# ─── Retention ─────────────────────────────────────────────────────────────


def test_staging_retention_default() -> None:
    assert Settings().staging_retention_days == 30


def test_object_storage_defaults() -> None:
    s = Settings()
    assert s.object_storage_backend == "minio"
    assert s.object_storage_endpoint == "localhost:9100"
    assert s.object_storage_presigned_endpoint is None
    assert s.object_storage_presign_mode == "proxy"
    assert s.object_storage_access_key.get_secret_value() == "minioadmin"
    assert s.object_storage_secret_key.get_secret_value() == "minioadmin"
    assert s.object_storage_bucket_name == "medtronics-storage"
    assert s.object_storage_secure is False
    assert s.object_storage_region == "us-east-1"
    assert s.object_storage_auto_create_bucket is True
    assert s.admin_file_allowed_prefix_set == frozenset(
        {"uploads", "source-documents", "media", "ingest", "module-thumbnails"}
    )
    assert s.admin_file_upload_prefix == "uploads"
    assert s.admin_file_presigned_max_seconds == 24 * 60 * 60


def test_object_storage_s3_forces_auto_create_off() -> None:
    s = Settings(
        object_storage_backend="s3",
        object_storage_endpoint=None,
        object_storage_access_key="",
        object_storage_secret_key="",
        object_storage_auto_create_bucket=True,
        object_storage_presign_mode="direct",
    )
    assert s.object_storage_backend == "s3"
    assert s.object_storage_auto_create_bucket is False


def test_object_storage_rejects_unknown_backend() -> None:
    with pytest.raises(ValidationError, match="OBJECT_STORAGE_BACKEND"):
        Settings(object_storage_backend="gcs")  # type: ignore[call-arg]


def test_admin_file_upload_prefix_must_be_allowlisted() -> None:
    with pytest.raises(ValidationError):
        Settings(admin_file_upload_prefix="not-allowed")  # type: ignore[call-arg]


def test_admin_file_upload_prefix_normalizes_slashes() -> None:
    s = Settings(admin_file_upload_prefix="/uploads/")  # type: ignore[call-arg]
    assert s.admin_file_upload_prefix == "uploads"


def test_coaching_rag_defaults() -> None:
    s = Settings()
    assert s.coaching_rag_module_limit == 5
    assert s.coaching_rag_presigned_url_ttl_seconds == 3600
    assert s.coaching_rag_context_max_chars == 28_000


def test_coaching_rag_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COACHING_RAG_MODULE_LIMIT", "10")
    monkeypatch.setenv("COACHING_RAG_PRESIGNED_URL_TTL_SECONDS", "7200")
    monkeypatch.setenv("COACHING_RAG_CONTEXT_MAX_CHARS", "50000")
    s = Settings()
    assert s.coaching_rag_module_limit == 10
    assert s.coaching_rag_presigned_url_ttl_seconds == 7200
    assert s.coaching_rag_context_max_chars == 50_000


# ─── Model selection ownership ─────────────────────────────────────────────


def test_platform_settings_do_not_own_model_selection() -> None:
    """Inference models/budgets live in ai-runtime generation_profiles."""
    s = Settings()
    for removed in (
        "ai_cloud_provider",
        "text_model",
        "vision_model",
        "identification_model",
        "embedding_model",
        "google_inference_model",
        "google_vision_model",
        "google_identification_model",
        "google_embedding_model",
        "stage_c_max_output_tokens",
        "stage_d_published_merge_max_output_tokens",
    ):
        assert not hasattr(s, removed), f"unexpected platform setting: {removed}"
    assert s.embedding_dimension == 768


# ─── env-override mechanics ────────────────────────────────────────────────


def test_env_override_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE_C_INSUFFICIENT_SOURCE_MIN_TOKENS", "750")
    s = Settings()
    assert s.stage_c_insufficient_source_min_tokens == 750
    assert isinstance(s.stage_c_insufficient_source_min_tokens, int)


def test_env_override_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUIZ_PASS_THRESHOLD_DEFAULT", "0.85")
    s = Settings()
    assert s.quiz_pass_threshold_default == 0.85
    assert isinstance(s.quiz_pass_threshold_default, float)


def test_invalid_float_env_value_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUIZ_PASS_THRESHOLD_DEFAULT", "not-a-float")
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_int_env_value_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAGE_C_CHUNK_TARGET_TOKENS", "not-a-number")
    with pytest.raises(ValidationError):
        Settings()


# ─── get_settings caching ──────────────────────────────────────────────────


def test_get_settings_returns_singleton() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b


def test_get_settings_picks_up_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_settings is cached, but the autouse fixture clears the cache
    between tests so env-var overrides take effect on a fresh call."""
    monkeypatch.setenv("STAGE_C_INSUFFICIENT_SOURCE_MIN_TOKENS", "999")
    s = get_settings()
    assert s.stage_c_insufficient_source_min_tokens == 999


# ─── SPICE referral safety ─────────────────────────────────────────────────


def test_spice_referral_set_parses_default_destinations() -> None:
    s = Settings()
    assert "Upazila Health Complex (UHC)" in s.spice_referral_set
    assert "BRAC Health Programme Clinic" in s.spice_referral_set
    assert "" not in s.spice_referral_set


# ─── Architecture-reset removals: removed attrs must NOT be present ────────


@pytest.mark.parametrize("app_env", ["production", "staging"])
def test_deployed_env_rejects_insecure_defaults(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    with pytest.raises(ValidationError):
        Settings(
            database_password=None,
            ai_runtime_token="dev-internal-token",
        )


@pytest.mark.parametrize("app_env", ["production", "staging"])
def test_deployed_env_requires_spice_tenant_id_map(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com")
    with pytest.raises(ValidationError, match="SPICE_TENANT_ID_MAP"):
        Settings(
            database_password="secure-password",
            ai_runtime_token="prod-token",
            object_storage_access_key="prod-access",
            object_storage_secret_key="prod-secret",
            spice_tenant_id_map="",
        )


@pytest.mark.parametrize(
    "removed_attr",
    [
        "generate_embeddings_on_publish",  # always-on via post-publish worker
        "outline_min_heading_count",  # Stage B deleted
        "outline_min_pages_with_heading_ratio",  # Stage B deleted
        "snippet_substantive_overlap_threshold",  # snippet system deleted
        "distractor_critique_min_score",  # distractor critique deleted
        "distractor_critique_max_attempts",  # distractor critique deleted
        "reviewer_claim_ttl_days",  # W-6 reviewer queue deleted
        "reviewer_token",  # W-6 reviewer auth deleted
    ],
)
def test_architecture_reset_removed_settings_are_gone(removed_attr: str) -> None:
    """If anyone re-introduces these, code paths that used to exist need
    to come back too — re-read docs/ARCHITECTURE_RESET.md before doing so."""
    s = Settings()
    assert not hasattr(s, removed_attr), (
        f"`{removed_attr}` was removed by the architecture reset; "
        "if you're adding it back, also revert the corresponding code "
        "deletion (W-6 reviewer queue, snippet system, distractor critique, "
        "Stage B outline parser, or the embedding feature flag)."
    )
