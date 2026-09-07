-- A driver's country, everywhere their preferred name is already shown.
--
-- users.country (0013) is not readable cross-driver today: unlike
-- driver_profiles (which has a "claim_status = 'claimed'" public branch),
-- users_select only ever admits the caller's own row or an admin. Widening
-- that with a new RLS policy would be the wrong tool here -- Postgres has
-- no way to scope a *policy* to only some columns, only the *role*'s grant
-- as a whole, and users already carries genuinely private columns (email,
-- date_of_birth, guardian_email, guardian_consent_status) that a new public
-- SELECT policy would expose right alongside country. So this follows the
-- same escape hatch driver_active_team_name (0011) already established for
-- an identical shape of problem: a small SECURITY DEFINER function that
-- returns just the one safe field. Exposing country this way is not a new
-- disclosure -- driver_profiles_select already makes a claimed profile's
-- name public to every signed-in driver; country is the same kind of fact
-- about the same already-public person.

-- ---------------------------------------------------------------------------
-- 1. driver_country: one profile's country, for embedding as a computed
--    column inside another query (mirrors driver_active_team_name's shape
--    exactly, used the same way inside track_driver_podium/track_summaries/
--    admin_list_orphaned_teams below).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION driver_country(p_driver_profile_id BIGINT)
RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT u.country FROM driver_profiles p JOIN users u ON u.id = p.user_id
     WHERE p.id = p_driver_profile_id;
$$;

REVOKE ALL ON FUNCTION driver_country(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION driver_country(BIGINT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 2. driver_countries: a batch lookup for a page that already has a list of
--    driver_profile_ids on screen (Home, Teams roster, session comparison)
--    and would otherwise need one round trip per row. Same safety
--    reasoning as (1) -- an id for a profile that isn't claimed/linked
--    just comes back with a NULL country, same as (1).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION driver_countries(p_driver_profile_ids BIGINT[])
RETURNS TABLE (driver_profile_id BIGINT, country TEXT)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT p.id, u.country
      FROM driver_profiles p
      JOIN users u ON u.id = p.user_id
     WHERE p.id = ANY(p_driver_profile_ids);
$$;

REVOKE ALL ON FUNCTION driver_countries(BIGINT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION driver_countries(BIGINT[]) TO authenticated;

-- ---------------------------------------------------------------------------
-- 3. track_summaries, track_driver_podium, track_team_podium: redefined
--    (CREATE OR REPLACE, full bodies from 0011) to add a country column
--    alongside each driver-name column they already return. Nothing else
--    about these three changes -- same predicates, same security mode,
--    same grants.
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS track_summaries(TEXT);
CREATE FUNCTION track_summaries(p_engine_category TEXT DEFAULT NULL)
RETURNS TABLE (
    track_name TEXT,
    session_count BIGINT,
    best_lap_s DOUBLE PRECISION,
    best_lap_driver_name TEXT,
    best_lap_driver_country TEXT,
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
           driver_country(bpt.driver_profile_id) AS best_lap_driver_country,
           bpt.engine_category AS best_lap_engine_category,
           AVG(sc.average_lap_s) AS average_lap_s,
           MAX(parse_session_date(sc.start_date)) AS last_driven_date
      FROM scoped sc
      LEFT JOIN best_per_track bpt ON bpt.track_name = sc.track_name
      LEFT JOIN driver_profiles p ON p.id = bpt.driver_profile_id
     GROUP BY sc.track_name, bpt.best_lap_s, p.display_name, bpt.engine_category, bpt.driver_profile_id
     ORDER BY sc.track_name;
$$;

REVOKE ALL ON FUNCTION track_summaries(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_summaries(TEXT) TO authenticated;

DROP FUNCTION IF EXISTS track_driver_podium(TEXT, TEXT, INT);
CREATE FUNCTION track_driver_podium(
    p_track_name TEXT,
    p_engine_category TEXT DEFAULT NULL,
    p_limit INT DEFAULT 10
)
RETURNS TABLE (
    rank BIGINT,
    driver_profile_id BIGINT,
    driver_name TEXT,
    driver_country TEXT,
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
           r.driver_profile_id, r.driver_name, driver_country(r.driver_profile_id),
           r.best_lap_s, r.engine_category,
           driver_active_team_name(r.driver_profile_id) AS team_name
      FROM ranked r
     ORDER BY r.best_lap_s ASC
     LIMIT GREATEST(p_limit, 0);
$$;

REVOKE ALL ON FUNCTION track_driver_podium(TEXT, TEXT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_driver_podium(TEXT, TEXT, INT) TO authenticated;

DROP FUNCTION IF EXISTS track_team_podium(TEXT, TEXT, INT);
CREATE FUNCTION track_team_podium(
    p_track_name TEXT,
    p_engine_category TEXT DEFAULT NULL,
    p_limit INT DEFAULT 3
)
RETURNS TABLE (
    rank BIGINT,
    team_id BIGINT,
    team_name TEXT,
    best_lap_s DOUBLE PRECISION,
    fastest_driver_name TEXT,
    fastest_driver_country TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    WITH per_driver AS (
        SELECT tm.team_id, t.name AS team_name, p.display_name AS driver_name,
               p.id AS driver_profile_id,
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
         GROUP BY tm.team_id, t.name, p.display_name, p.id
    ),
    fastest_per_team AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY best_lap_s ASC) AS rn
          FROM per_driver
    )
    SELECT ROW_NUMBER() OVER (ORDER BY best_lap_s ASC) AS rank,
           team_id, team_name, best_lap_s, driver_name AS fastest_driver_name,
           driver_country(driver_profile_id) AS fastest_driver_country
      FROM fastest_per_team
     WHERE rn = 1
     ORDER BY best_lap_s ASC
     LIMIT GREATEST(p_limit, 0);
$$;

REVOKE ALL ON FUNCTION track_team_podium(TEXT, TEXT, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION track_team_podium(TEXT, TEXT, INT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 4. admin_user_overview / admin_list_orphaned_teams: both already
--    SECURITY DEFINER and admin-gated, so this is a plain additive column
--    -- no new grant, no driver_country() call needed for the first (it
--    already reads straight off `users`).
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS admin_user_overview();
CREATE FUNCTION admin_user_overview()
RETURNS TABLE (
    id BIGINT,
    email TEXT,
    display_name TEXT,
    country TEXT,
    engine_category TEXT,
    is_admin BOOLEAN,
    email_verified BOOLEAN,
    is_linked BOOLEAN,
    guardian_consent_status TEXT,
    created_at TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    session_count BIGINT,
    upload_count BIGINT,
    lap_count BIGINT,
    last_session_at TIMESTAMPTZ
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT is_app_admin() THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT u.id,
           u.email,
           u.display_name,
           u.country,
           u.engine_category,
           u.is_admin,
           u.email_verified,
           -- An account with no external_auth_id authenticates fine and is
           -- invisible to every policy. Worth surfacing here, because it is
           -- otherwise only discoverable by the person hitting it.
           u.external_auth_id IS NOT NULL,
           u.guardian_consent_status,
           u.created_at,
           u.last_login_at,
           -- Sessions they uploaded or that are filed under their profile,
           -- counted once even when both are true.
           (SELECT count(DISTINCT s.id) FROM sessions s
             WHERE s.uploaded_by_user_id = u.id
                OR s.driver_profile_id IN (SELECT p.id FROM driver_profiles p WHERE p.user_id = u.id)),
           (SELECT count(*) FROM upload_batches b WHERE b.uploaded_by_user_id = u.id),
           (SELECT count(*) FROM laps l
             WHERE l.session_db_id IN (
                 SELECT s.id FROM sessions s
                  WHERE s.uploaded_by_user_id = u.id
                     OR s.driver_profile_id IN (SELECT p.id FROM driver_profiles p WHERE p.user_id = u.id)
             )),
           (SELECT max(s.ingested_at) FROM sessions s
             WHERE s.uploaded_by_user_id = u.id
                OR s.driver_profile_id IN (SELECT p.id FROM driver_profiles p WHERE p.user_id = u.id))
      FROM users u
     ORDER BY u.created_at DESC;
END;
$$;

REVOKE ALL ON FUNCTION admin_user_overview() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_user_overview() TO authenticated;

CREATE OR REPLACE FUNCTION admin_list_orphaned_teams()
RETURNS TABLE (id BIGINT, name TEXT, members JSONB)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT is_app_admin() THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT t.id, t.name,
           COALESCE((
               SELECT jsonb_agg(
                          jsonb_build_object(
                              'membership_id', tm.id, 'display_name', p.display_name,
                              'country', driver_country(p.id), 'role', tm.role
                          )
                          ORDER BY tm.role
                      )
                 FROM team_memberships tm
                 JOIN driver_profiles p ON p.id = tm.driver_profile_id
                WHERE tm.team_id = t.id AND tm.status = 'active'
           ), '[]'::jsonb)
      FROM teams t
     WHERE NOT EXISTS (
         SELECT 1 FROM team_memberships tm2
          WHERE tm2.team_id = t.id AND tm2.role = 'manager' AND tm2.status = 'active'
     );
END;
$$;

REVOKE ALL ON FUNCTION admin_list_orphaned_teams() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_list_orphaned_teams() TO authenticated;
