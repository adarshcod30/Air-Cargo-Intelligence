-- Allowlisted read views. The query path may touch NOTHING else.
--
-- Two reasons this exists rather than querying the tables directly:
--
-- 1. A generated query can only reach what a view exposes, so the blast
--    radius of a bad plan is bounded by definition rather than by care.
-- 2. Citations stop being optional. Every row a caller can retrieve
--    carries the source document behind it, so an answer that omits its
--    provenance is impossible to construct rather than merely discouraged.

CREATE OR REPLACE VIEW v_cargo_fact AS
SELECT
    f.fact_id,
    f.grain::text                                   AS grain,
    p.period_label                                  AS period,
    p.period_kind,
    p.calendar_year,
    p.calendar_month,
    p.sort_key,
    f.direction::text                               AS direction,
    f.measure,
    ap.iata_code                                    AS airport_iata,
    ap.icao_code                                    AS airport_icao,
    ap.airport_name,
    ap.city                                         AS airport_city,
    ap.country,
    al.airline_name,
    al.is_aggregate                                 AS airline_is_aggregate,
    f.tonnage_kg,
    f.prior_year_tonnage_kg,
    f.reported_change_pct,
    f.publisher,
    -- Provenance travels with every row, so a caller cannot obtain a
    -- number without also obtaining the document that supports it.
    f.source_document_id,
    sd.title                                        AS source_title,
    sd.source_url,
    sd.retrieved_at
FROM fact_cargo_movement f
JOIN dim_period p       ON p.period_id = f.period_id
JOIN source_document sd ON sd.source_document_id = f.source_document_id
LEFT JOIN dim_airport ap ON ap.airport_id = f.airport_id
LEFT JOIN dim_airline al ON al.airline_id = f.airline_id;


CREATE OR REPLACE VIEW v_trend AS
SELECT
    t.grain::text        AS grain,
    t.entity_key,
    t.direction::text    AS direction,
    p.period_label       AS period,
    p.sort_key,
    t.tonnage_kg,
    t.yoy_pct,
    t.mom_pct,
    t.cagr_pct,
    t.share_of_total,
    t.share_shift_pp,
    ap.airport_name,
    al.airline_name
FROM trend t
JOIN dim_period p ON p.period_id = t.period_id
LEFT JOIN dim_airport ap ON ap.iata_code = t.entity_key
LEFT JOIN dim_airline al ON al.airline_name = t.entity_key;


CREATE OR REPLACE VIEW v_anomaly AS
SELECT
    a.anomaly_id,
    a.grain::text        AS grain,
    a.entity_key,
    a.direction::text    AS direction,
    p.period_label       AS period,
    p.sort_key,
    a.observed_kg,
    a.expected_kg,
    a.deviation_pct,
    a.z_score,
    a.method,
    a.severity,
    a.evidence_fact_ids,
    a.detected_at,
    ap.airport_name,
    al.airline_name
FROM anomaly a
JOIN dim_period p ON p.period_id = a.period_id
LEFT JOIN dim_airport ap ON ap.iata_code = a.entity_key
LEFT JOIN dim_airline al ON al.airline_name = a.entity_key;


CREATE OR REPLACE VIEW v_forecast AS
SELECT
    f.forecast_id,
    f.grain::text        AS grain,
    f.entity_key,
    f.direction::text    AS direction,
    f.period_label       AS period,
    f.horizon,
    f.predicted_kg,
    f.lower_kg,
    f.upper_kg,
    f.model,
    f.backtest_mape,
    ap.airport_name,
    al.airline_name
FROM forecast f
LEFT JOIN dim_airport ap ON ap.iata_code = f.entity_key
LEFT JOIN dim_airline al ON al.airline_name = f.entity_key;


CREATE OR REPLACE VIEW v_source AS
SELECT
    sd.source_document_id,
    sd.doc_key,
    sd.publisher,
    sd.title,
    sd.source_url,
    sd.media_type,
    sd.byte_size,
    sd.retrieved_at,
    count(f.fact_id) AS fact_count
FROM source_document sd
LEFT JOIN fact_cargo_movement f ON f.source_document_id = sd.source_document_id
GROUP BY sd.source_document_id;
