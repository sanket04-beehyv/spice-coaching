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
    countIf(event_type = 'module_quiz_attempted') AS quiz_attempts,
    countIf(event_type = 'module_quiz_attempted' AND outcome = 'correct') AS quiz_correct,
    countIf(event_type = 'digital_help_used') AS digital_help_used,
    countIf(event_type = 'spice_action_observed' AND outcome = 'incorrect') AS incorrect_referrals,

    count() AS total_events
FROM coaching_events
GROUP BY tenant_id, chw_id, village_id, upazila_id, event_date;

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
