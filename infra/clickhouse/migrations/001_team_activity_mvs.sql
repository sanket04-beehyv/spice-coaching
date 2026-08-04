-- One-time migration for existing ClickHouse deployments.
-- Adds quiz_views to chw_daily_summary and creates chw_digital_help_daily MV.
-- Run during a maintenance window: DROP/recreate briefly stops MV ingestion.

DROP VIEW IF EXISTS chw_daily_summary;

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

-- Backfill target tables from historical coaching_events (idempotent if re-run after truncate).
INSERT INTO chw_daily_summary
SELECT
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

INSERT INTO chw_digital_help_daily
SELECT
    tenant_id,
    chw_id,
    module_id,
    event_date,
    count() AS query_count
FROM coaching_events
WHERE event_type = 'digital_help_used'
GROUP BY tenant_id, chw_id, module_id, event_date;
