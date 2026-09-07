-- Driver Rating: a competitive, skill-based number (mu/sigma, displayed as
-- mu - k*sigma) built from field-relative pace comparisons, kept strictly
-- separate from a purely-motivational streak/activity layer. See
-- telemetry/rating/ for the batch job that populates everything here --
-- nothing in this migration is written by a client. Every threshold that
-- shapes those computations lives in telemetry/rating/config.py (batch
-- side) and web/src/lib/rating.ts (display-side decay/provisional math),
-- not in this file, per the brief's "keep every threshold in configuration"
-- requirement -- this migration only defines where the *results* land.
--
-- Nothing here is computed synchronously on upload. scripts/compute_ratings.py
-- runs as a separate batch/background job (own process, own schedule),
-- deliberately -- cohort-based updates need other drivers' sessions to
-- already exist, and a single new fast lap should not visibly ripple
-- through everyone's numbers in real time.

-- ---------------------------------------------------------------------------
-- 1. Part 1 (validity gate): per-lap status, feeding everything downstream.
--
-- Deliberately separate from the existing `is_outlier`/`excluded_by_user`
-- columns rather than overloading them -- those are "does this lap count
-- for this driver's own best/average stats" (already driving the whole
-- rest of the app); this is "is this lap trustworthy enough to compare
-- across drivers for a competitive number", a stricter and separate
-- question. A lap can be a perfectly good personal-stats lap while still
-- being held for review here (e.g. a suspected track cut).
-- ---------------------------------------------------------------------------

ALTER TABLE laps ADD COLUMN IF NOT EXISTS validity_status TEXT NOT NULL DEFAULT 'excluded'
    CHECK (validity_status IN ('verified', 'flagged', 'excluded'));
ALTER TABLE laps ADD COLUMN IF NOT EXISTS validity_reason TEXT;
ALTER TABLE laps ADD COLUMN IF NOT EXISTS validity_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_laps_validity_status ON laps (validity_status);

-- ---------------------------------------------------------------------------
-- 2. Track reference lines: the consensus racing-line corridor a lap's GPS
--    path is checked against for a likely track cut. One row per track --
--    a track's boundary doesn't change with class or conditions, so unlike
--    the pace reference below this is not split by engine_category/
--    track_condition. Built from many verified laps' `lap_traces` rows
--    (median position + per-point tolerance from the spread), so an
--    individual noisy lap can't distort it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS track_reference_lines (
    track_name TEXT PRIMARY KEY,
    -- Parallel arrays, one entry per resampled point along the lap
    -- (uniform arc-length fraction 0..1, not raw distance -- lap lengths
    -- vary lap to lap even at the same track).
    ref_lat DOUBLE PRECISION[] NOT NULL,
    ref_lon DOUBLE PRECISION[] NOT NULL,
    ref_tolerance_m DOUBLE PRECISION[] NOT NULL,
    built_from_lap_count INTEGER NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 3. Track pace reference: the Part 2 fallback for a session with no
--    comparable field that day. One row per track+class+conditions bucket,
--    refreshed nightly (not live) -- a robust reference (median of the top
--    several verified sessions), plus an implied opponent rating derived
--    from the drivers who contributed to it, so the reference can be
--    treated as a single virtual opponent in the same Elo-expected-score
--    formula the field-based path uses.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS track_pace_reference (
    track_name TEXT NOT NULL,
    engine_category TEXT NOT NULL DEFAULT '',
    track_condition TEXT NOT NULL DEFAULT '',
    reference_lap_s DOUBLE PRECISION NOT NULL,
    implied_rating DOUBLE PRECISION NOT NULL,
    sample_session_count INTEGER NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (track_name, engine_category, track_condition)
);

-- ---------------------------------------------------------------------------
-- 4. Driver ratings: the competitive number. mu/sigma only -- the
--    conservative displayed rating (mu - k*sigma_effective) is computed at
--    read time (web/src/lib/rating.ts), not stored, because sigma keeps
--    growing between sessions purely as a function of elapsed time; storing
--    a "current" sigma would mean either a mutating value with no session
--    to explain the mutation (breaking Part 5's explainability) or a daily
--    cron whose only job is advancing a clock. sigma_at_last_update /
--    last_verified_session_at are the two facts decay is a pure function
--    of; everything else here is snapshot-of-last-batch-run state.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS driver_ratings (
    driver_profile_id BIGINT PRIMARY KEY REFERENCES driver_profiles (id) ON DELETE CASCADE,
    mu DOUBLE PRECISION NOT NULL DEFAULT 1500,
    sigma_at_last_update DOUBLE PRECISION NOT NULL DEFAULT 350,
    last_verified_session_at TIMESTAMPTZ,
    sessions_rated_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 5. Driver rating history: one row per session that moved a driver's
--    rating -- Part 5's explainability requirement. Never overwritten,
--    only appended to; UNIQUE(driver_profile_id, session_db_id) is also
--    what makes a re-run of the batch job idempotent (a session that has
--    already contributed an update is skipped, not re-applied).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS driver_rating_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    driver_profile_id BIGINT NOT NULL REFERENCES driver_profiles (id) ON DELETE CASCADE,
    session_db_id BIGINT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mechanism TEXT NOT NULL CHECK (mechanism IN ('field', 'reference')),
    cohort_track TEXT,
    cohort_date DATE,
    cohort_conditions TEXT,
    cohort_class TEXT,
    -- Other drivers compared against -- 0 for a reference-based row (the
    -- "opponent" there is the historical bucket, not a cohort of peers).
    cohort_size INTEGER NOT NULL DEFAULT 0,
    mu_before DOUBLE PRECISION NOT NULL,
    mu_after DOUBLE PRECISION NOT NULL,
    sigma_before DOUBLE PRECISION NOT NULL,
    sigma_after DOUBLE PRECISION NOT NULL,
    note TEXT,
    UNIQUE (driver_profile_id, session_db_id)
);

CREATE INDEX IF NOT EXISTS idx_driver_rating_history_driver
    ON driver_rating_history (driver_profile_id, computed_at DESC);

-- ---------------------------------------------------------------------------
-- 6. Weekly activity + streaks (Part 4) -- the always-positive layer,
--    architecturally separate tables from everything above so it is
--    structurally impossible for a rating query to accidentally join
--    against streak state. Monday-start week, matching a driver who
--    realistically gets out once a week rather than a daily-streak
--    fitness-app shape.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS driver_weekly_activity (
    driver_profile_id BIGINT NOT NULL REFERENCES driver_profiles (id) ON DELETE CASCADE,
    week_start DATE NOT NULL,
    verified_session_count INTEGER NOT NULL DEFAULT 0,
    -- Cosmetic only -- see telemetry/rating/streaks.py. Never feeds mu/sigma.
    has_midweek_bonus BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (driver_profile_id, week_start)
);

CREATE TABLE IF NOT EXISTS driver_streaks (
    driver_profile_id BIGINT PRIMARY KEY REFERENCES driver_profiles (id) ON DELETE CASCADE,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    freezes_available INTEGER NOT NULL DEFAULT 0,
    last_qualifying_week DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- RLS.
--
-- Nothing here has a client-facing write policy at all -- every one of
-- these tables is written exclusively by scripts/compute_ratings.py on the
-- service-role connection, which bypasses RLS entirely. Not "revoke then
-- narrow" (0006's pattern for a column a driver IS meant to write); there
-- is no column here a client should ever write directly, so the default
-- GRANT ALL is revoked outright and nothing is granted back beyond SELECT.
--
-- driver_ratings and the two track_* tables are read-open to any
-- authenticated user (USING (true), same shape as `teams_select`): the
-- displayed rating is the leaderboard number, and the track-level
-- reference artifacts are what the "field-based vs reference-based"
-- explainability badge needs to be inspectable by anyone, not just the
-- driver in question.
--
-- driver_rating_history / driver_weekly_activity / driver_streaks stay
-- owner + admin only, same ownership check as driver_profiles_update_own
-- (0013): the per-session breakdown of *why* a rating moved, and the
-- day-by-day activity/streak detail, are for that driver's own eyes (and
-- admin support), not a second public feed alongside the leaderboard --
-- nothing in the brief asks for other drivers' streaks to be visible.
-- ---------------------------------------------------------------------------

ALTER TABLE track_reference_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE track_pace_reference ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_ratings ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_rating_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_weekly_activity ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_streaks ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON track_reference_lines FROM anon, authenticated;
REVOKE ALL ON track_pace_reference FROM anon, authenticated;
REVOKE ALL ON driver_ratings FROM anon, authenticated;
REVOKE ALL ON driver_rating_history FROM anon, authenticated;
REVOKE ALL ON driver_weekly_activity FROM anon, authenticated;
REVOKE ALL ON driver_streaks FROM anon, authenticated;

GRANT SELECT ON track_reference_lines TO authenticated;
GRANT SELECT ON track_pace_reference TO authenticated;
GRANT SELECT ON driver_ratings TO authenticated;
GRANT SELECT ON driver_rating_history TO authenticated;
GRANT SELECT ON driver_weekly_activity TO authenticated;
GRANT SELECT ON driver_streaks TO authenticated;

DROP POLICY IF EXISTS track_reference_lines_select ON track_reference_lines;
CREATE POLICY track_reference_lines_select ON track_reference_lines
    FOR SELECT USING (true);

DROP POLICY IF EXISTS track_pace_reference_select ON track_pace_reference;
CREATE POLICY track_pace_reference_select ON track_pace_reference
    FOR SELECT USING (true);

DROP POLICY IF EXISTS driver_ratings_select ON driver_ratings;
CREATE POLICY driver_ratings_select ON driver_ratings
    FOR SELECT USING (true);

DROP POLICY IF EXISTS driver_rating_history_select ON driver_rating_history;
CREATE POLICY driver_rating_history_select ON driver_rating_history
    FOR SELECT USING (
        is_app_admin()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = driver_rating_history.driver_profile_id
              AND p.user_id = current_app_user_id()
        )
    );

DROP POLICY IF EXISTS driver_weekly_activity_select ON driver_weekly_activity;
CREATE POLICY driver_weekly_activity_select ON driver_weekly_activity
    FOR SELECT USING (
        is_app_admin()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = driver_weekly_activity.driver_profile_id
              AND p.user_id = current_app_user_id()
        )
    );

DROP POLICY IF EXISTS driver_streaks_select ON driver_streaks;
CREATE POLICY driver_streaks_select ON driver_streaks
    FOR SELECT USING (
        is_app_admin()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = driver_streaks.driver_profile_id
              AND p.user_id = current_app_user_id()
        )
    );
