-- Persist the analysis output as queryable rows, so the raw dataframe blob
-- stops being the only place it exists.
--
-- Today `session_cache.dataframe_parquet` holds each session's entire parsed
-- dataframe as BYTEA -- 3-5 MB per session, ~46 MB per track day, against a
-- 500 MB database. Two problems follow from that, and they turn out to be
-- the same problem:
--
--   1. Capacity. Roughly ten track days fills the database.
--   2. Lap Analysis cannot be built on the Next.js frontend at all. Every
--      trace, delta and sector time it draws lives inside that blob, and a
--      browser cannot read Parquet out of BYTEA over PostgREST. `laps` holds
--      only lap_number/lap_time_s/is_outlier.
--
-- Measured on the bundled 11-session export: everything the analysis
-- produces, stored as the tables below, comes to ~4% of the blob it was
-- derived from (~0.15 MB vs ~4 MB per session). So this both unblocks the
-- page and returns most of the space.
--
-- Raw data is not lost by dropping a blob afterwards. Uploads land in the
-- `telemetry` Storage bucket and are never deleted, so any session carrying
-- an `upload_batch_id` can be re-parsed from its original TSV. Sessions that
-- predate the upload pipeline have no such object -- `session_cache
-- .raw_storage_path` below is where their archived Parquet goes before the
-- blob is cleared, so re-analysis stays possible for every session either
-- way. See scripts/backfill_analysis.py, which refuses to clear a blob it
-- cannot account for.
--
-- Additive: three new tables, two new nullable columns. Nothing existing is
-- dropped or rewritten; clearing blobs is a separate, opt-in script.

-- ---------------------------------------------------------------------------
-- Session-level analysis: one row per session, mirroring SessionAnalysis.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS session_analysis (
    session_db_id BIGINT PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,

    -- Bumped when the analysis changes shape or a threshold moves, so a
    -- backfill can find rows computed by an older version rather than
    -- guessing from analyzed_at.
    analysis_version INTEGER NOT NULL DEFAULT 1,

    best_lap INTEGER,
    theoretical_best_s DOUBLE PRECISION,
    -- Some exports fill Latitude/Longitude on every fix but never the GPS
    -- Speed channel, in which case speed is derived from GPS Distance.
    -- Worth carrying: it qualifies every speed-based figure downstream.
    speed_is_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    clean_lap_numbers INTEGER[],

    -- JSONB rather than columns because these are read whole and never
    -- filtered on: the lap summary, the segment map the reference line was
    -- built from, the per-segment session bests, and the setup engine's
    -- hypotheses (whose shape varies by `area`).
    summary JSONB,
    segments JSONB,
    best_segment_times JSONB,
    setup_suggestions JSONB,

    -- Set when a session has no clean laps at all -- a legitimate outcome of
    -- a short or aborted run, not an error. Callers check this rather than
    -- assuming best_lap is populated, exactly as the Streamlit pages do.
    data_error TEXT,
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Per-lap, per-segment times -- the sector table and the "where the time
-- went" breakdown.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lap_segment_times (
    session_db_id BIGINT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL,
    segment_index INTEGER NOT NULL,
    segment_label TEXT,
    segment_kind TEXT,
    time_s DOUBLE PRECISION,
    PRIMARY KEY (session_db_id, lap_number, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_lap_segment_times_session
    ON lap_segment_times (session_db_id);

-- ---------------------------------------------------------------------------
-- Per-lap traces.
--
-- Stored as parallel float8 arrays per lap rather than a row per sample.
-- A lap is ~310 GPS fixes, so a row-per-sample table would be ~6k rows per
-- session and tens of millions across a season, to always be read back whole
-- and in order. Arrays keep one row per lap, keep the samples in order by
-- construction, and are a fifth of the size.
--
-- `lap_time_s` is elapsed time within the lap. Storing it (rather than a
-- precomputed delta against some fixed reference) is what lets the delta
-- trace between *any* two laps be derived later by interpolating both onto a
-- common distance grid -- which is exactly what `cross_session_delta_trace`
-- does today, and it means changing the reference lap needs no new data.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lap_traces (
    session_db_id BIGINT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    lap_number INTEGER NOT NULL,
    sample_count INTEGER NOT NULL,

    distance_m DOUBLE PRECISION[] NOT NULL,
    lap_time_s DOUBLE PRECISION[] NOT NULL,
    speed_kmh DOUBLE PRECISION[],
    rpm DOUBLE PRECISION[],
    latitude DOUBLE PRECISION[],
    longitude DOUBLE PRECISION[],
    -- Inferred, not measured: this logger has no brake or throttle channel
    -- (see telemetry/metrics.py's add_braking_throttle_estimates).
    braking BOOLEAN[],
    power_on BOOLEAN[],

    PRIMARY KEY (session_db_id, lap_number)
);

-- ---------------------------------------------------------------------------
-- The raw dataframe becomes optional.
-- ---------------------------------------------------------------------------

-- Where the archived Parquet went, for sessions whose raw data is not
-- otherwise recoverable from an uploaded TSV in Storage.
ALTER TABLE session_cache ADD COLUMN IF NOT EXISTS raw_storage_path TEXT;

-- A cleared blob is the whole point, so the column can no longer be NOT
-- NULL. Guarded because a database where it is already nullable (or where
-- this migration has run before) must not error.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'session_cache'
          AND column_name = 'dataframe_parquet' AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE session_cache ALTER COLUMN dataframe_parquet DROP NOT NULL;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- RLS: all three inherit their session's visibility.
--
-- Same shape as `laps_select` and `session_cache_select` in 0001: the
-- EXISTS subquery is itself filtered by the `sessions` policy, so a session
-- the caller cannot see yields no row here either. Writes are the worker's
-- alone, on the service-role connection -- no INSERT/UPDATE/DELETE policy is
-- defined, so a client cannot forge an analysis result or a trace.
-- ---------------------------------------------------------------------------

ALTER TABLE session_analysis ENABLE ROW LEVEL SECURITY;
ALTER TABLE lap_segment_times ENABLE ROW LEVEL SECURITY;
ALTER TABLE lap_traces ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS session_analysis_select ON session_analysis;
CREATE POLICY session_analysis_select ON session_analysis
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM sessions s WHERE s.id = session_analysis.session_db_id)
    );

DROP POLICY IF EXISTS lap_segment_times_select ON lap_segment_times;
CREATE POLICY lap_segment_times_select ON lap_segment_times
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM sessions s WHERE s.id = lap_segment_times.session_db_id)
    );

DROP POLICY IF EXISTS lap_traces_select ON lap_traces;
CREATE POLICY lap_traces_select ON lap_traces
    FOR SELECT USING (
        EXISTS (SELECT 1 FROM sessions s WHERE s.id = lap_traces.session_db_id)
    );
