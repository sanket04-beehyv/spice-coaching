-- Unattributed module-demand events for daily creation-suggestion jobs.
-- Projects digital_help_used (no module_id) and free-text module_requested
-- (no module_id, non-empty requested_module_name) into a thin event table.

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

-- Backfill from historical coaching_events (idempotent if re-run after truncate).
INSERT INTO unattributed_module_demand_events
SELECT
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
