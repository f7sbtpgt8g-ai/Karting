-- Make Row Level Security actually hold, ahead of the Next.js frontend.
--
-- A new file rather than an edit to 0001_init.sql, deliberately: editing
-- that file in place is what broke logins once already (see commit 82e3dba
-- -- a project on an older copy had no supported way to pick up the change).
-- Migrations are append-only from here.
--
-- Context: until now nothing has ever queried this database as `anon` or
-- `authenticated`. The Streamlit app connects on a superuser connection that
-- bypasses RLS, and no account has ever had an `external_auth_id`, so
-- `current_app_user_id()` has always returned NULL and every policy has
-- silently evaluated to "deny". Pointing a browser client at PostgREST makes
-- these policies load-bearing for the first time. tests/test_rls_policies.py
-- exercises each one as a real client would; it found everything below.
--
-- Two classes of problem, both fixed here:
--
--   1. Six tables had no RLS at all. In Supabase that is not "closed by
--      default" -- `anon` and `authenticated` hold default grants on every
--      table in `public`, and RLS only ever *restricts* an existing grant.
--      So `auth_tokens` (password-reset and email-verification tokens),
--      `auth_sessions` (live login tokens) and `email_outbox` (every message
--      body, including reset links) were readable by any authenticated user
--      the moment PostgREST was exposed. That is account takeover, and it is
--      the single most important thing in this file.
--
--   2. Every policy was FOR SELECT, so no client write was possible at all --
--      a driver could not change their own session's visibility, nor ask to
--      join a team. RLS-only reads with no write policies looks secure and
--      is, but it also means the app cannot function.

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

-- The current viewer's driver profile. SECURITY DEFINER for the same reason
-- current_app_user_id() is: policies on driver_profiles would otherwise
-- recurse through a policy that queries driver_profiles.
CREATE OR REPLACE FUNCTION current_app_profile_id() RETURNS BIGINT
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT id FROM driver_profiles WHERE user_id = current_app_user_id();
$$;

-- ---------------------------------------------------------------------------
-- 1. Server-only tables: no client should ever read these.
--
-- RLS enabled with NO policy at all is a deny-all for anon/authenticated,
-- while the service-role/superuser connection the Python app and the Part 4
-- worker use continues to bypass RLS entirely. The REVOKEs are defence in
-- depth: if a policy is ever added to one of these by mistake, the missing
-- grant still stops a client reaching it.
-- ---------------------------------------------------------------------------

ALTER TABLE auth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_outbox ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON auth_tokens FROM anon, authenticated;
REVOKE ALL ON auth_sessions FROM anon, authenticated;
REVOKE ALL ON email_outbox FROM anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. Attribution / claim tables: real client reads, tightly scoped.
--
-- A driver needs to see requests aimed at them (someone else uploaded a
-- session and says it is theirs) and requests they raised themselves.
-- Nothing else.
-- ---------------------------------------------------------------------------

ALTER TABLE attribution_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_claim_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE attribution_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS attribution_requests_select ON attribution_requests;
CREATE POLICY attribution_requests_select ON attribution_requests
    FOR SELECT USING (
        target_driver_profile_id = current_app_profile_id()
        OR requested_by_user_id = current_app_user_id()
    );

-- Accepting or rejecting is the target driver's call, and theirs only.
DROP POLICY IF EXISTS attribution_requests_update_target ON attribution_requests;
CREATE POLICY attribution_requests_update_target ON attribution_requests
    FOR UPDATE USING (target_driver_profile_id = current_app_profile_id())
    WITH CHECK (target_driver_profile_id = current_app_profile_id());

DROP POLICY IF EXISTS profile_claim_requests_select ON profile_claim_requests;
CREATE POLICY profile_claim_requests_select ON profile_claim_requests
    FOR SELECT USING (requested_by_user_id = current_app_user_id());

DROP POLICY IF EXISTS profile_claim_requests_insert_own ON profile_claim_requests;
CREATE POLICY profile_claim_requests_insert_own ON profile_claim_requests
    FOR INSERT WITH CHECK (requested_by_user_id = current_app_user_id());

DROP POLICY IF EXISTS attribution_reports_select_own ON attribution_reports;
CREATE POLICY attribution_reports_select_own ON attribution_reports
    FOR SELECT USING (reported_by_user_id = current_app_user_id());

DROP POLICY IF EXISTS attribution_reports_insert_own ON attribution_reports;
CREATE POLICY attribution_reports_insert_own ON attribution_reports
    FOR INSERT WITH CHECK (reported_by_user_id = current_app_user_id());

-- ---------------------------------------------------------------------------
-- 3. Sessions: reads stay as 0001 defined them, but require a signed-in
--    viewer, and add the writes the app actually performs.
--
-- 0001's read policy had no authentication requirement on its
-- "publicly shared" branch, so an anonymous caller with the anon key could
-- enumerate every shared session. That may become a deliberate choice later
-- (a public leaderboard), but it should be a decision rather than a
-- side effect -- today the Streamlit app puts all of this behind login.
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS sessions_select ON sessions;
CREATE POLICY sessions_select ON sessions
    FOR SELECT USING (
        current_app_user_id() IS NOT NULL
        AND (
            EXISTS (
                SELECT 1 FROM driver_profiles p
                WHERE p.id = sessions.driver_profile_id
                  AND sessions.visibility = 'shared'
                  AND sessions.attribution_status = 'confirmed'
                  AND p.claim_status = 'claimed'
                  AND p.user_id IS NOT NULL
            )
            OR uploaded_by_user_id = current_app_user_id()
            OR EXISTS (
                SELECT 1 FROM driver_profiles p
                WHERE p.id = sessions.driver_profile_id AND p.user_id = current_app_user_id()
            )
            OR (
                sessions.visibility IN ('team', 'shared')
                AND sessions.attribution_status = 'confirmed'
                AND EXISTS (
                    SELECT 1 FROM driver_profiles p
                    WHERE p.id = sessions.driver_profile_id
                      AND p.claim_status = 'claimed' AND p.user_id IS NOT NULL
                )
                AND shares_active_team_with(sessions.driver_profile_id)
            )
        )
    );

-- Changing your own session's sharing tier is the app's most common write.
-- WITH CHECK repeats the ownership test so a driver cannot re-own a session
-- to someone else in the same statement.
DROP POLICY IF EXISTS sessions_update_own ON sessions;
CREATE POLICY sessions_update_own ON sessions
    FOR UPDATE
    USING (
        uploaded_by_user_id = current_app_user_id()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = sessions.driver_profile_id AND p.user_id = current_app_user_id()
        )
    )
    WITH CHECK (
        uploaded_by_user_id = current_app_user_id()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = sessions.driver_profile_id AND p.user_id = current_app_user_id()
        )
    );

DROP POLICY IF EXISTS sessions_delete_own ON sessions;
CREATE POLICY sessions_delete_own ON sessions
    FOR DELETE USING (
        uploaded_by_user_id = current_app_user_id()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = sessions.driver_profile_id AND p.user_id = current_app_user_id()
        )
    );

-- Sessions are created by the ingest worker on the service-role connection,
-- never by a browser client, so there is deliberately no INSERT policy here.

-- ---------------------------------------------------------------------------
-- 4. Teams: a driver may ask to join, and may leave. Everything that changes
--    someone else's standing (accepting a request, promoting, removing) is a
--    manager/admin action and stays server-side for now, so the team feature
--    can grow without a client ever being able to grant itself membership.
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS team_memberships_insert_request ON team_memberships;
CREATE POLICY team_memberships_insert_request ON team_memberships
    FOR INSERT WITH CHECK (
        -- Only ever a pending request, only ever as a plain member, and only
        -- ever for your own driver profile. Without the status/role pinning,
        -- a client could insert itself as an active manager and immediately
        -- read every team member's team-visible telemetry.
        status = 'pending'
        AND role = 'member'
        AND driver_profile_id = current_app_profile_id()
    );

DROP POLICY IF EXISTS team_memberships_leave_own ON team_memberships;
CREATE POLICY team_memberships_leave_own ON team_memberships
    FOR UPDATE
    USING (driver_profile_id = current_app_profile_id())
    WITH CHECK (
        driver_profile_id = current_app_profile_id()
        -- Leaving or withdrawing only -- never self-promotion to active.
        AND status IN ('left', 'rejected')
    );

-- ---------------------------------------------------------------------------
-- 5. Kart setups: 0001 matched on the driver's *display name*, so two
--    drivers who happen to share a name could read each other's setups.
--    Re-scoped to setups belonging to a session the viewer actually owns,
--    matched on the same (source_file, session_index, start_time) identity
--    triple storage.py uses.
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS kart_setups_select ON kart_setups;
CREATE POLICY kart_setups_select ON kart_setups
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE p.user_id = current_app_user_id()
              AND s.source_file IS NOT DISTINCT FROM kart_setups.source_file
              AND s.session_index IS NOT DISTINCT FROM kart_setups.session_index
              AND s.start_time IS NOT DISTINCT FROM kart_setups.start_time
        )
    );

DROP POLICY IF EXISTS kart_setups_insert_own ON kart_setups;
CREATE POLICY kart_setups_insert_own ON kart_setups
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE p.user_id = current_app_user_id()
              AND s.source_file IS NOT DISTINCT FROM kart_setups.source_file
              AND s.session_index IS NOT DISTINCT FROM kart_setups.session_index
              AND s.start_time IS NOT DISTINCT FROM kart_setups.start_time
        )
    );
