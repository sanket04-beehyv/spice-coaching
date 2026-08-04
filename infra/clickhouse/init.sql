DROP TABLE IF EXISTS coaching_events;

CREATE TABLE IF NOT EXISTS coaching_events (
    id                      String,
    event_schema_version    UInt16,
    sdk_version             LowCardinality(String),
    session_id              Nullable(String),
    patient_visit_id        Nullable(String),
    patient_track_id        Nullable(String),
    patient_id_hash         Nullable(String),
    chw_id                  Int64,
    tenant_id               UUID,
    village_id              Nullable(String),
    upazila_id              Nullable(String),
    event_family            LowCardinality(String),
    event_type              LowCardinality(String),
    module_family_id        Nullable(UUID),
    module_id               Nullable(UUID),
    card_family_id          Nullable(UUID),
    quiz_id                 Nullable(UUID),
    module_version          Nullable(Int32),
    quiz_score_pct          Nullable(Float64),
    clinical_domain         LowCardinality(Nullable(String)),
    card_type               LowCardinality(Nullable(String)),
    trigger_type            LowCardinality(Nullable(String)),
    inference_mode          LowCardinality(Nullable(String)),
    outcome                 LowCardinality(Nullable(String)),
    validator_status        LowCardinality(Nullable(String)),
    fallback_used           Nullable(UInt8),
    network_state           LowCardinality(Nullable(String)),
    payload_json            String DEFAULT '{}',
    event_date              Date,
    timestamp_utc           DateTime64(3),
    timestamp_local         DateTime64(3),
    synced_at               DateTime64(3) DEFAULT now64(),
    inserted_at             DateTime64(3) DEFAULT now64()
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, chw_id, event_date, id)
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW IF NOT EXISTS chw_daily_summary
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, chw_id, event_date)
AS SELECT
    tenant_id,
    chw_id,
    village_id,
    upazila_id,
    event_date,

    countIf(event_type = 'module_card_viewed') AS cards_shown,
    countIf(event_type = 'module_quiz_viewed') AS quiz_views,
    countIf(event_type = 'module_quiz_attempted') AS quiz_attempts,
    countIf(event_type = 'module_quiz_attempted' AND outcome = 'correct') AS quiz_correct,
    countIf(event_type = 'digital_help_used') AS digital_help_used,
    countIf(event_type = 'spice_action_observed' AND outcome = 'incorrect') AS incorrect_referrals,

    count() AS total_events
FROM coaching_events
GROUP BY tenant_id, chw_id, village_id, upazila_id, event_date;

CREATE MATERIALIZED VIEW IF NOT EXISTS chw_digital_help_daily
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, chw_id, event_date)
AS SELECT
    tenant_id,
    chw_id,
    module_id,
    event_date,
    count() AS query_count
FROM coaching_events
WHERE event_type = 'digital_help_used'
GROUP BY tenant_id, chw_id, module_id, event_date;

CREATE MATERIALIZED VIEW IF NOT EXISTS llm_daily_summary
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date)
AS SELECT
    tenant_id,
    event_date,

    count() AS digital_help_event_count,
    countIf(inference_mode = 'online') AS inference_online_count,
    countIf(inference_mode = 'edge') AS inference_edge_count,
    countIf(inference_mode = 'offline') AS inference_offline_count,
    countIf(validator_status = 'pass') AS validator_pass_count,
    countIf(validator_status = 'fail') AS validator_fail_count,
    countIf(fallback_used = 1) AS fallback_used_count
FROM coaching_events
WHERE event_type = 'digital_help_used'
GROUP BY tenant_id, event_date;

CREATE TABLE IF NOT EXISTS unattributed_module_demand_events (
    id                      String,
    tenant_id               UUID,
    chw_id                  Int64,
    event_date              Date,
    source                  LowCardinality(String),
    text                    String,
    normalized_text         String,
    timestamp_utc           DateTime64(3),
    inserted_at             DateTime64(3) DEFAULT now64()
)
ENGINE = ReplacingMergeTree(inserted_at)
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, event_date, source, id)
SETTINGS index_granularity = 8192;

CREATE MATERIALIZED VIEW IF NOT EXISTS unattributed_module_demand_events_mv
TO unattributed_module_demand_events
AS SELECT
    id,
    tenant_id,
    chw_id,
    event_date,
    if(event_type = 'digital_help_used', 'digital_help', 'module_requested') AS source,
    if(
        event_type = 'digital_help_used',
        coalesce(
            nullIf(JSONExtractString(payload_json, 'question'), ''),
            nullIf(JSONExtractString(payload_json, 'query'), '')
        ),
        nullIf(JSONExtractString(payload_json, 'requested_module_name'), '')
    ) AS text,
    lowerUTF8(
        replaceRegexpAll(
            trimBoth(
                if(
                    event_type = 'digital_help_used',
                    coalesce(
                        nullIf(JSONExtractString(payload_json, 'question'), ''),
                        nullIf(JSONExtractString(payload_json, 'query'), '')
                    ),
                    nullIf(JSONExtractString(payload_json, 'requested_module_name'), '')
                )
            ),
            '\\s+',
            ' '
        )
    ) AS normalized_text,
    timestamp_utc,
    now64() AS inserted_at
FROM coaching_events
WHERE module_id IS NULL
  AND (
    (
        event_type = 'digital_help_used'
        AND length(
            trimBoth(
                coalesce(
                    nullIf(JSONExtractString(payload_json, 'question'), ''),
                    nullIf(JSONExtractString(payload_json, 'query'), '')
                )
            )
        ) > 0
    )
    OR (
        event_type = 'module_requested'
        AND length(trimBoth(JSONExtractString(payload_json, 'requested_module_name'))) > 0
    )
  );

-- Knowledge-document view rollups (document_viewed telemetry).
-- Grain keeps chw_id so unique-user counts can be computed without scanning raw events.
-- upazila_id stays in the grain so geography rollups remain accurate when present.
CREATE MATERIALIZED VIEW IF NOT EXISTS document_view_daily
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(event_date)
ORDER BY (tenant_id, source_document_id, chw_id, upazila_id, event_date)
AS SELECT
    tenant_id,
    JSONExtractString(payload_json, 'source_document_id') AS source_document_id,
    chw_id,
    -- Non-null key: Nullable upazila_id cannot be in SummingMergeTree ORDER BY.
    ifNull(upazila_id, '') AS upazila_id,
    event_date,
    count() AS view_count
FROM coaching_events
WHERE event_type = 'document_viewed'
  AND JSONExtractString(payload_json, 'source_document_id') != ''
GROUP BY
    tenant_id,
    source_document_id,
    chw_id,
    upazila_id,
    event_date;
