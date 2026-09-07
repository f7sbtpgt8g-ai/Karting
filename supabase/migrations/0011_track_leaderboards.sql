-- Tracks: a track list (aggregated over sessions.track_name -- there is no
-- `tracks` table, "same track" is exact string equality), and per-track
-- leaderboards grouped by sessions.engine_category (the live field a
-- BEFORE INSERT trigger fills from the driver's own setting at upload time
-- -- 0009 -- not the legacy free-text kart_class nothing in web/ writes to
-- any more).
--
-- Every function here is a plain read, and defaults to SECURITY INVOKER
-- (stated explicitly, matching this schema's convention of never leaving
-- the security mode implicit). RLS (0002's sessions_select) already makes
-- "your own sessions, your team's team-or-shared sessions, and everyone's
-- publicly shared sessions" visible to any signed-in caller -- most of what
-- follows needs nothing more than that.
--
-- Two exceptions need SECURITY DEFINER, for two different reasons:
--
-- track_driver_podium and track_team_podium must not simply trust "whatever
-- RLS lets this caller see" for which *sessions* count, even though they
-- stay SECURITY INVOKER (the first) or are DEFINER for an unrelated reason
-- (the second, below) -- RLS's 'team'+'shared' branch additionally lets a
-- caller see their own team's team-only sessions, which must never leak
-- into a podium that is supposed to be identical for every viewer. Both
-- functions repeat the public predicate explicitly in their WHERE clause
-- (visibility='shared', not IN ('team','shared')) rather than leaning on
-- RLS to have already narrowed the rows -- this mirrors
-- telemetry/accounts.py's PUBLIC_VISIBILITY_SQL / the (now-dead)
-- leaderboard()/team_leaderboard() methods, reimplemented here since the
-- web app has no service-role connection to run that Python from at all.
--
-- Separately, driver_active_team_name and track_team_podium must run as
-- SECURITY DEFINER because team_memberships_select's RLS policy only lets a
-- caller read membership rows for teams *they* belong to -- under plain
-- SECURITY INVOKER, track_team_podium's own ranking join against
-- team_memberships would silently collapse to "my team, maybe" for every
-- caller, and track_driver_podium's team_name display column would read
-- NULL for anyone not on the caller's own team. Both are safe to widen:
-- team names (teams_select is USING (true)) and the fact that a claimed
-- driver plays for some team are not confidential, and the sessions/lap
-- times these expose are already gated to the same public predicate above.

-- ---------------------------------------------------------------------------
-- 0. Supporting index: every function below filters (track_name,
--    engine_category) and orders by best_lap_s. idx_sessions_track_name
--    (0001) only covers the first column.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_sessions_track_engine_best
    ON sessions (track_name, engine_category, best_lap_s)
    WHERE track_name IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 1. parse_session_date: sessions.start_date is free text, written by the
--    parser as DD-MM-YYYY, but not every historical row is guaranteed to
--    match that shape. to_date() raises on anything that doesn't fit its
--    format string, and a single malformed date must not take down a whole
--    track's aggregate row -- so this returns NULL for anything that
--    doesn't match. IMMUTABLE: a pure function of its input.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION parse_session_date(p_raw TEXT)
RETURNS DATE
LANGUAGE sql
IMMUTABLE
SET search_path = public
AS $$
    SELECT CASE WHEN p_raw ~ '^\d{2}-\d{2}-\d{4}$' THEN to_date(p_raw, 'DD-MM-YYYY') ELSE NULL END;
$$;

REVOKE ALL ON FUNCTION parse_session_date(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION parse_session_date(TEXT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 2. track_summaries: the Tracks list. One row per track_name, scoped to
--    whatever RLS already lets the caller see (own + team-shared + public
--    shared) -- SECURITY INVOKER, no predicate repeated here, because unlike
--    the podiums below this is not meant to be one canonical answer for
--    every viewer; it is "the tracks *you* have data for", the same way
--    Home is "your sessions", just not narrowed to a single driver.
--
--    p_engine_category NULL means "outright best regardless of class" --
--    best_lap_engine_category then tells the caller which class actually
--    ran it, so the UI can show a class tag next to an unfiltered row.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION track_summaries(p_engine_category TEXT DEFAULT NULL)
RETURNS TABLE (
    track_name TEXT,
    session_count BIGINT,
    best_lap_s DOUBLE PRECISION,
    best_lap_driver_name TEXT,
    best_lap_engine_category TEXT,
    average_lap_s DOUBLE PRECISION,
    last_driven_date DATE
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    WITH scoped AS (
        SELECT s.track_name, s.best_lap_s, s.average_lap_s, s.engine_category,
               s.driver_profile_id, s.start_date
          FROM sessions s
         WHERE s.track_name IS NOT NULL
           AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
    ),
    best_per_track AS (
        -- One row per track: the fastest session in scope, and whose class
        -- it was run in. DISTINCT ON needs its own ORDER BY track_name,
        -- best_lap_s -- ties break arbitrarily but deterministically per
        -- Postgres's own tie-break (row order), which is fine here: this is
        -- "a" fastest driver for that lap time, not a ranked field.
        SELECT DISTINCT ON (track_name)
               track_name, best_lap_s, engine_category, driver_profile_id
          FROM scoped
         WHERE best_lap_s IS NOT NULL
         ORDER BY track_name, best_lap_s ASC
    )
    SELECT sc.track_name,
           COUNT(*) AS session_count,
           bpt.best_lap_s,
           p.display_name AS best_lap_driver_name,
           bpt.engine_category AS best_lap_engine_category,
           AVG(sc.average_lap_s) AS average_lap_s,
           MAX(parse_session_date(sc.start_date)) AS last_driven_date
      FROM scoped sc
      LEFT JOIN best_per_track bpt ON bpt.track_name = sc.track_name
      LEFT JOIN driver_profiles p ON p.id = bpt.driver_profile_id
     GROUP BY sc.track_name, bpt.best_lap_s, p.display_name, bpt.engine_category
     ORDER BY sc.track_name;
$$;

REVOKE ALL ON FUNCTION track_summaries(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_summaries(TEXT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 3. track_my_best: the caller's own best/average at one track, one class.
--    Filters driver_profile_id = current_app_profile_id() explicitly rather
--    than leaning on RLS's ownership branch to have already narrowed it --
--    it happens to be the same rows either way, but this is the one case
--    where "whatever RLS lets me see, further filtered to mine" is exactly
--    the intended meaning, so writing it out is correct, not incidental.
--    A plain aggregate with no GROUP BY always returns exactly one row
--    (all NULL if nothing matches) -- the client always gets data[0].
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION track_my_best(p_track_name TEXT, p_engine_category TEXT DEFAULT NULL)
RETURNS TABLE (
    best_lap_s DOUBLE PRECISION,
    average_lap_s DOUBLE PRECISION,
    session_count BIGINT,
    last_driven_date DATE
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    SELECT MIN(s.best_lap_s),
           AVG(s.average_lap_s),
           COUNT(*),
           MAX(parse_session_date(s.start_date))
      FROM sessions s
     WHERE s.driver_profile_id = current_app_profile_id()
       AND s.track_name = p_track_name
       AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
       AND s.best_lap_s IS NOT NULL;
$$;

REVOKE ALL ON FUNCTION track_my_best(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_my_best(TEXT, TEXT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 4. driver_active_team_name: a driver's current active team name, if any.
--    SECURITY DEFINER because team_memberships_select only lets a caller
--    read membership rows for teams *they* belong to -- without this,
--    track_driver_podium's team_name column would silently read as NULL
--    for every driver not on the caller's own team. Safe to widen: team
--    name (teams_select is USING (true)) and the fact that a claimed driver
--    plays for some team are not confidential.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION driver_active_team_name(p_driver_profile_id BIGINT)
RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT t.name FROM team_memberships tm JOIN teams t ON t.id = tm.team_id
     WHERE tm.driver_profile_id = p_driver_profile_id AND tm.status = 'active'
     LIMIT 1;
$$;

REVOKE ALL ON FUNCTION driver_active_team_name(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION driver_active_team_name(BIGINT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 5. track_driver_podium: every driver's best at one track/class, public-
--    predicate filtered so this is the same list for every caller
--    regardless of team membership. team_name is a display extra via the
--    helper above, not part of ranking.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION track_driver_podium(
    p_track_name TEXT,
    p_engine_category TEXT DEFAULT NULL,
    p_limit INT DEFAULT 10
)
RETURNS TABLE (
    rank BIGINT,
    driver_profile_id BIGINT,
    driver_name TEXT,
    best_lap_s DOUBLE PRECISION,
    engine_category TEXT,
    team_name TEXT
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
    WITH ranked AS (
        SELECT p.id AS driver_profile_id,
               p.display_name AS driver_name,
               MIN(s.best_lap_s) AS best_lap_s,
               -- The class that driver's own best lap here was run in, not
               -- just any session of theirs at this track.
               (ARRAY_AGG(s.engine_category ORDER BY s.best_lap_s ASC))[1] AS engine_category
          FROM sessions s
          JOIN driver_profiles p ON p.id = s.driver_profile_id
         WHERE s.track_name = p_track_name
           AND s.best_lap_s IS NOT NULL
           AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
           AND s.visibility = 'shared'
           AND s.attribution_status = 'confirmed'
           AND p.claim_status = 'claimed'
           AND p.user_id IS NOT NULL
         GROUP BY p.id, p.display_name
    )
    SELECT ROW_NUMBER() OVER (ORDER BY r.best_lap_s ASC) AS rank,
           r.driver_profile_id, r.driver_name, r.best_lap_s, r.engine_category,
           driver_active_team_name(r.driver_profile_id) AS team_name
      FROM ranked r
     ORDER BY r.best_lap_s ASC
     LIMIT GREATEST(p_limit, 0);
$$;

REVOKE ALL ON FUNCTION track_driver_podium(TEXT, TEXT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_driver_podium(TEXT, TEXT, INT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 6. track_team_podium: each team's fastest member at one track/class.
--    SECURITY DEFINER -- unlike track_driver_podium (which only needs
--    team_memberships for a display-only name lookup, delegated to the
--    helper above), this function's *ranking itself* joins team_memberships
--    across every team, not just the caller's own -- under SECURITY INVOKER
--    that join would be RLS-filtered down to at most the caller's own team,
--    silently turning a "top 3 teams" podium into "my team, maybe". The
--    public predicate (visibility='shared' etc.) still applies, same as
--    track_driver_podium -- this widens *whose team rows* are readable, not
--    *which sessions* count.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION track_team_podium(
    p_track_name TEXT,
    p_engine_category TEXT DEFAULT NULL,
    p_limit INT DEFAULT 3
)
RETURNS TABLE (
    rank BIGINT,
    team_id BIGINT,
    team_name TEXT,
    best_lap_s DOUBLE PRECISION,
    fastest_driver_name TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH per_driver AS (
        SELECT tm.team_id, t.name AS team_name, p.display_name AS driver_name,
               MIN(s.best_lap_s) AS best_lap_s
          FROM sessions s
          JOIN driver_profiles p ON p.id = s.driver_profile_id
          JOIN team_memberships tm ON tm.driver_profile_id = p.id AND tm.status = 'active'
          JOIN teams t ON t.id = tm.team_id
         WHERE s.track_name = p_track_name
           AND s.best_lap_s IS NOT NULL
           AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
           AND s.visibility = 'shared'
           AND s.attribution_status = 'confirmed'
           AND p.claim_status = 'claimed'
           AND p.user_id IS NOT NULL
         GROUP BY tm.team_id, t.name, p.display_name
    ),
    fastest_per_team AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY best_lap_s ASC) AS rn
          FROM per_driver
    )
    SELECT ROW_NUMBER() OVER (ORDER BY best_lap_s ASC) AS rank,
           team_id, team_name, best_lap_s, driver_name AS fastest_driver_name
      FROM fastest_per_team
     WHERE rn = 1
     ORDER BY best_lap_s ASC
     LIMIT GREATEST(p_limit, 0);
$$;

REVOKE ALL ON FUNCTION track_team_podium(TEXT, TEXT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_team_podium(TEXT, TEXT, INT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 7. track_map_source: which session + lap to draw the circuit from.
--    Prefers the fastest public-eligible session (same predicate as the
--    podiums, so the shape shown matches "the" podium's track, not a
--    stranger's private line) and falls back to the caller's own fastest
--    session at this track when no public one exists yet -- SECURITY
--    INVOKER, so that fallback can never return a session RLS would refuse
--    the caller anyway; it is a convenience ordering, not a widened grant.
--    Preferring public over "my own, even if faster" is deliberate: the
--    map should look the same for everyone who opens this track, not flip
--    based on who's currently the fastest visitor to the page.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION track_map_source(p_track_name TEXT, p_engine_category TEXT DEFAULT NULL)
RETURNS TABLE (session_id BIGINT, lap_number INT)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_session_id BIGINT;
    v_lap_number INT;
BEGIN
    SELECT s.id, sa.best_lap
      INTO v_session_id, v_lap_number
      FROM sessions s
      JOIN driver_profiles p ON p.id = s.driver_profile_id
      JOIN session_analysis sa ON sa.session_db_id = s.id
     WHERE s.track_name = p_track_name
       AND s.best_lap_s IS NOT NULL
       AND sa.best_lap IS NOT NULL
       AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
       AND s.visibility = 'shared'
       AND s.attribution_status = 'confirmed'
       AND p.claim_status = 'claimed'
       AND p.user_id IS NOT NULL
     ORDER BY s.best_lap_s ASC
     LIMIT 1;

    IF v_session_id IS NULL THEN
        SELECT s.id, sa.best_lap
          INTO v_session_id, v_lap_number
          FROM sessions s
          JOIN session_analysis sa ON sa.session_db_id = s.id
         WHERE s.track_name = p_track_name
           AND s.driver_profile_id = current_app_profile_id()
           AND s.best_lap_s IS NOT NULL
           AND sa.best_lap IS NOT NULL
           AND (p_engine_category IS NULL OR s.engine_category = p_engine_category)
         ORDER BY s.best_lap_s ASC
         LIMIT 1;
    END IF;

    RETURN QUERY SELECT v_session_id, v_lap_number;
END;
$$;

REVOKE ALL ON FUNCTION track_map_source(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_map_source(TEXT, TEXT) TO authenticated;
