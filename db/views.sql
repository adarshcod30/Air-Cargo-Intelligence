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


-- Explanations, joined to the anomaly they were written against so a
-- reader gets the claim and its evidence in one row.
CREATE OR REPLACE VIEW v_insight AS
SELECT
    i.insight_id,
    i.headline,
    i.narrative,
    i.citations,
    i.created_at,
    a.anomaly_id,
    a.entity_key,
    a.grain::text        AS grain,
    a.direction::text    AS direction,
    a.severity,
    p.period_label       AS period,
    a.observed_kg,
    a.expected_kg,
    a.deviation_pct
FROM insight i
LEFT JOIN anomaly a    ON a.anomaly_id = i.anomaly_id
LEFT JOIN dim_period p ON p.period_id  = a.period_id;


-- ---------------------------------------------------------------------------
-- Agent traces
--
-- Exposed as views for the same reason the facts are: the serving role holds
-- no rights on any base table, so a query that escapes the compiler still
-- cannot reach an unaudited row. The console reads only these.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_agent_run AS
SELECT
    r.agent_run_id,
    r.trace_id,
    r.agent,
    r.goal,
    r.policy,
    r.succeeded,
    r.steps,
    r.result_summary,
    r.started_at,
    r.finished_at,
    r.elapsed_ms,
    r.input_tokens,
    r.output_tokens,
    r.fallback_steps,
    -- A run configured for a model but decided entirely by the fallback is
    -- not a model run. Surfacing that here means the console cannot
    -- accidentally present one as the other.
    -- Classified on what the steps positively declare, not on the absence
    -- of a fallback marker. Counting `fallback_steps = 0` as evidence of
    -- model judgement inverted the labels outright: a pure-heuristic run
    -- has no fallbacks either, so it read as 'model', while a run that fell
    -- back on every single step read as 'heuristic'.
    CASE
        WHEN r.steps = 0 THEN 'none'
        WHEN (SELECT count(*) FROM agent_step s
               WHERE s.agent_run_id = r.agent_run_id
                 AND coalesce(s.policy, '') <> '') = 0 THEN 'unrecorded'
        WHEN (SELECT count(*) FROM agent_step s
               WHERE s.agent_run_id = r.agent_run_id
                 AND s.policy LIKE 'llm%') = 0 THEN 'heuristic'
        WHEN (SELECT count(*) FROM agent_step s
               WHERE s.agent_run_id = r.agent_run_id
                 AND s.policy NOT LIKE 'llm%'
                 AND coalesce(s.policy, '') <> '') = 0 THEN 'model'
        ELSE 'mixed'
    END                                              AS effective_policy,
    (SELECT count(*) FROM agent_step s
      WHERE s.agent_run_id = r.agent_run_id AND NOT s.ok) AS failed_steps
FROM agent_run r;


CREATE OR REPLACE VIEW v_agent_step AS
SELECT
    s.agent_step_id,
    s.agent_run_id,
    r.trace_id,
    r.agent,
    s.seq,
    s.tool,
    s.args,
    s.ok,
    s.observation,
    s.reasoning,
    s.policy,
    s.elapsed_ms
FROM agent_step s
JOIN agent_run r ON r.agent_run_id = s.agent_run_id;


-- Tool-level behaviour, which is the evidence for whether the loop is
-- actually reflecting: a tool with retries and a non-zero failure rate is
-- one the agent had to recover from.
CREATE OR REPLACE VIEW v_agent_tool_stats AS
SELECT
    s.tool,
    count(*)                                              AS calls,
    sum(CASE WHEN s.ok THEN 1 ELSE 0 END)                 AS ok_calls,
    sum(CASE WHEN s.ok THEN 0 ELSE 1 END)                 AS failed_calls,
    round(avg(s.elapsed_ms))                              AS avg_ms,
    max(s.elapsed_ms)                                     AS max_ms,
    count(DISTINCT s.agent_run_id)                        AS runs_using
FROM agent_step s
GROUP BY s.tool;


-- Dropped rather than replaced: CREATE OR REPLACE cannot insert a column
-- into the middle of an existing view's column list.
DROP VIEW IF EXISTS v_document_chunk;
CREATE VIEW v_document_chunk AS
SELECT
    c.chunk_id,
    c.source_document_id,
    c.seq,
    c.page,
    c.content,
    c.token_estimate,
    -- The vector is exposed deliberately: retrieval runs in the API, which
    -- holds no rights on any base table, so the similarity operator has to
    -- be applicable to something the serving role can actually select.
    c.embedding,
    c.embed_model,
    d.title       AS document_title,
    d.publisher,
    d.source_url
FROM document_chunk c
JOIN source_document d ON d.source_document_id = c.source_document_id;


-- Corpus term statistics. Query-time weights must be derived from the same
-- frequencies the index was built with, so the serving role needs to read
-- them - through a view, like everything else it reads.
CREATE OR REPLACE VIEW v_rag_vocab AS
SELECT token, document_frequency, total_documents FROM rag_vocab;


-- ---------------------------------------------------------------------------
-- Operating metrics and the efficiency measures derived from them
--
-- Cargo load factor is freight tonne-kilometres over available tonne-
-- kilometres: output delivered against capacity offered. It separates growth
-- that came from flying more from growth that came from filling what was
-- already flying, which tonnage alone cannot distinguish.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_operating_metric AS
SELECT
    m.metric_id,
    m.grain,
    m.entity_key,
    p.period_label            AS period,
    p.sort_key,
    m.direction,
    m.metric,
    m.value,
    m.unit,
    m.source_document_id
FROM fact_operating_metric m
JOIN dim_period p ON p.period_id = m.period_id;


-- One row per carrier, period and direction, with the metrics pivoted into
-- columns so the ratios can be expressed directly. A carrier missing either
-- side of a ratio yields NULL rather than a fabricated denominator.
CREATE OR REPLACE VIEW v_cargo_efficiency AS
WITH pivoted AS (
    SELECT
        m.grain,
        m.entity_key,
        m.period_id,
        m.direction,
        max(m.value) FILTER (WHERE m.metric = 'ftk_million')        AS ftk_million,
        max(m.value) FILTER (WHERE m.metric = 'atk_million')        AS atk_million,
        max(m.value) FILTER (WHERE m.metric = 'freight_tonnes')     AS freight_tonnes,
        max(m.value) FILTER (WHERE m.metric = 'mail_tonnes')        AS mail_tonnes,
        max(m.value) FILTER (WHERE m.metric = 'cargo_total_tonnes') AS cargo_total_tonnes,
        max(m.value) FILTER (WHERE m.metric = 'departures')         AS departures,
        max(m.value) FILTER (WHERE m.metric = 'pax_carried')        AS pax_carried,
        max(m.value) FILTER (WHERE m.metric = 'pax_load_factor')    AS pax_load_factor,
        max(m.value) FILTER (WHERE m.metric = 'block_hours')        AS block_hours
    FROM fact_operating_metric m
    GROUP BY m.grain, m.entity_key, m.period_id, m.direction
)
SELECT
    v.grain,
    v.entity_key,
    p.period_label  AS period,
    p.sort_key,
    v.direction,
    v.ftk_million,
    v.atk_million,
    v.freight_tonnes,
    v.mail_tonnes,
    v.cargo_total_tonnes,
    v.departures,
    v.pax_carried,
    v.pax_load_factor,
    -- The headline efficiency measure.
    round(100.0 * v.ftk_million / nullif(v.atk_million, 0), 2)      AS cargo_load_factor_pct,
    -- Tonnes of freight per departure: how much each flight actually lifted.
    round(v.freight_tonnes / nullif(v.departures, 0), 3)            AS tonnes_per_departure,
    -- Mail is a distinct product with its own economics; its share moves
    -- independently of general freight.
    round(100.0 * v.mail_tonnes / nullif(v.cargo_total_tonnes, 0), 2) AS mail_share_pct,
    -- Freight carried per passenger flown, which is what belly-hold
    -- dependency looks like at the carrier level.
    round(v.freight_tonnes / nullif(v.pax_carried, 0) * 1000, 3)    AS kg_freight_per_pax
FROM pivoted v
JOIN dim_period p ON p.period_id = v.period_id;
