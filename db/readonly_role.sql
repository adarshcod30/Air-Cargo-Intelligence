-- A read-only role for the query path (SRS NFR-6).
--
-- The semantic layer already restricts which relations a generated query
-- may name, and that is the useful first line. But it is defence in the
-- application: if any code path ever reached raw SQL, a connection owning
-- the schema could drop it. Two layers were designed and only one was
-- built, so this is the second.
--
-- The role can read the allowlisted views and nothing else. It has
-- no rights on the base tables, so a query that escapes the compiler
-- still cannot see an unaudited row, and no statement it issues can write.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aci_readonly') THEN
        CREATE ROLE aci_readonly LOGIN PASSWORD NULL;
    END IF;
END $$;

-- Start from nothing rather than from whatever PUBLIC happens to grant.
REVOKE ALL ON SCHEMA public FROM aci_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM aci_readonly;

GRANT CONNECT ON DATABASE air_cargo TO aci_readonly;
GRANT USAGE ON SCHEMA public TO aci_readonly;

GRANT SELECT ON v_cargo_fact TO aci_readonly;
GRANT SELECT ON v_trend      TO aci_readonly;
GRANT SELECT ON v_anomaly    TO aci_readonly;
GRANT SELECT ON v_forecast   TO aci_readonly;
GRANT SELECT ON v_source     TO aci_readonly;
GRANT SELECT ON v_insight    TO aci_readonly;

-- Agent traces. Read-only like everything else: the console displays what
-- the agents decided, it never edits a trace.
GRANT SELECT ON v_agent_run        TO aci_readonly;
GRANT SELECT ON v_agent_step       TO aci_readonly;
GRANT SELECT ON v_agent_tool_stats TO aci_readonly;
GRANT SELECT ON v_document_chunk   TO aci_readonly;
GRANT SELECT ON v_rag_vocab        TO aci_readonly;

-- Operating metrics and the efficiency ratios derived from them.
GRANT SELECT ON v_operating_metric TO aci_readonly;
GRANT SELECT ON v_cargo_efficiency TO aci_readonly;
GRANT SELECT ON v_airport_efficiency TO aci_readonly;
GRANT SELECT ON v_pipeline_state   TO aci_readonly;

-- A view runs with its owner's rights, so reading through them does not
-- require any grant on the tables underneath. Future tables must not be
-- granted by default either.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM aci_readonly;
