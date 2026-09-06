-- Administration: who the admins are, what they can see, and what deleting
-- a user actually means.
--
-- No separate admin login. Admins sign in the same way everyone else does
-- and carry a flag -- a second credential system would be a second auth
-- surface to secure, with its own password reset, its own session handling
-- and its own way to get it wrong, to protect strictly more than the first
-- one protects.

-- ---------------------------------------------------------------------------
-- The flag.
--
-- Deliberately not settable from the app: it is not in the column-level
-- UPDATE grant 0007 handed to `authenticated` (engine_category and
-- display_name only), so no client -- admin or not -- can grant it. The
-- first admin is made with SQL by whoever holds database access, and every
-- one after that the same way. That is the whole point: privilege comes from
-- outside the application, so a flaw inside it cannot mint an admin.
--
--   UPDATE users SET is_admin = TRUE WHERE email = 'you@example.com';
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

/**
 * Whether the caller is an admin.
 *
 * SECURITY DEFINER for the same reason `is_active_team_member` is: it reads
 * `users`, and it is called from a policy on `users`, which would otherwise
 * recurse.
 */
CREATE OR REPLACE FUNCTION is_app_admin()
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE(
        (SELECT u.is_admin FROM users u WHERE u.id = current_app_user_id()),
        FALSE
    );
$$;

-- Admins read every account. Everyone else still sees only their own row --
-- `users_select_self` from 0001 is left exactly as it was, and this is an
-- additional policy, so the two OR together.
DROP POLICY IF EXISTS users_select_admin ON users;
CREATE POLICY users_select_admin ON users
    FOR SELECT USING (is_app_admin());

-- ---------------------------------------------------------------------------
-- The admin listing.
--
-- A function rather than a view. In PostgreSQL 15+ a view runs with its
-- *owner's* permissions unless `security_invoker` is set, so a plain view
-- over `users` would hand every account to anyone who queried it -- exactly
-- the failure this whole schema is arranged to avoid. A SECURITY DEFINER
-- function with an explicit check is the same power with the check written
-- down where it can be read and tested.
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS admin_user_overview();
CREATE FUNCTION admin_user_overview()
RETURNS TABLE (
    id BIGINT,
    email TEXT,
    display_name TEXT,
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

-- ---------------------------------------------------------------------------
-- Deleting a user.
--
-- "Delete a user" is not one DELETE. `users` is referenced by eleven
-- columns across nine tables, most without ON DELETE CASCADE, so the naive
-- version fails on a foreign key -- and the version that only removes the
-- `users` row leaves their telemetry in place and lets them sign in again to
-- a freshly created empty account, because the Supabase Auth identity is a
-- separate object this schema does not own.
--
-- So this walks the graph in dependency order and removes the auth identity
-- too. What it deliberately does NOT delete:
--
--   * teams they created -- other people's memberships hang off those, and
--     removing a team to remove one member is not what anyone means;
--   * driver profiles they created for *other* drivers, which carry those
--     drivers' sessions. Their `created_by_user_id` is nulled instead.
--
-- Guarded three ways: caller must be an admin, may not delete themselves
-- (an admin locking themselves out mid-operation is the one mistake with no
-- in-app recovery), and may not remove the last admin.
-- ---------------------------------------------------------------------------

DROP FUNCTION IF EXISTS admin_delete_user(BIGINT);
CREATE FUNCTION admin_delete_user(target_user_id BIGINT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_caller     BIGINT := current_app_user_id();
    v_profiles   BIGINT[];
    v_sessions   BIGINT[];
    v_external   TEXT;
    v_email      TEXT;
    v_sessions_n INT := 0;
    v_auth_gone  BOOLEAN := FALSE;
BEGIN
    IF NOT is_app_admin() THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    IF target_user_id = v_caller THEN
        RAISE EXCEPTION 'You cannot delete your own account from here.'
            USING ERRCODE = '22023';
    END IF;

    SELECT external_auth_id, email INTO v_external, v_email
      FROM users WHERE id = target_user_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No such user' USING ERRCODE = '02000';
    END IF;

    IF (SELECT is_admin FROM users WHERE id = target_user_id)
       AND (SELECT count(*) FROM users WHERE is_admin) <= 1 THEN
        RAISE EXCEPTION 'That is the only admin account left.' USING ERRCODE = '22023';
    END IF;

    SELECT array_agg(id) INTO v_profiles FROM driver_profiles WHERE user_id = target_user_id;
    v_profiles := COALESCE(v_profiles, ARRAY[]::BIGINT[]);

    SELECT array_agg(id) INTO v_sessions
      FROM sessions
     WHERE uploaded_by_user_id = target_user_id
        OR driver_profile_id = ANY (v_profiles);
    v_sessions := COALESCE(v_sessions, ARRAY[]::BIGINT[]);
    v_sessions_n := array_length(v_sessions, 1);

    -- Rows referencing those sessions that do NOT cascade.
    DELETE FROM attribution_requests WHERE session_db_id = ANY (v_sessions);
    DELETE FROM attribution_reports  WHERE session_db_id = ANY (v_sessions);
    UPDATE pattern_instances SET reference_session_db_id = NULL
     WHERE reference_session_db_id = ANY (v_sessions);

    -- Sessions cascade to laps, session_cache, corner_metrics,
    -- pattern_instances, lap_traces, lap_segment_times and session_analysis.
    DELETE FROM sessions WHERE id = ANY (v_sessions);

    -- Upload batches: any session still referencing one is somebody else's,
    -- so the link is cut rather than the session removed.
    UPDATE sessions SET upload_batch_id = NULL
     WHERE upload_batch_id IN (SELECT id FROM upload_batches WHERE uploaded_by_user_id = target_user_id);
    DELETE FROM upload_batches WHERE uploaded_by_user_id = target_user_id;
    UPDATE upload_batches SET driver_profile_id = NULL WHERE driver_profile_id = ANY (v_profiles);

    -- Anything else pointing at their profiles or at them.
    DELETE FROM attribution_requests
     WHERE target_driver_profile_id = ANY (v_profiles) OR requested_by_user_id = target_user_id;
    DELETE FROM profile_claim_requests
     WHERE driver_profile_id = ANY (v_profiles) OR requested_by_user_id = target_user_id;
    DELETE FROM attribution_reports
     WHERE driver_profile_id = ANY (v_profiles) OR reported_by_user_id = target_user_id;
    DELETE FROM team_memberships WHERE driver_profile_id = ANY (v_profiles);
    UPDATE team_memberships SET decided_by_user_id = NULL WHERE decided_by_user_id = target_user_id;

    -- Their own profiles go; profiles they created for other drivers stay,
    -- because those carry other people's sessions.
    DELETE FROM driver_profiles WHERE user_id = target_user_id;
    UPDATE driver_profiles SET created_by_user_id = NULL WHERE created_by_user_id = target_user_id;

    -- Teams they founded outlive them.
    UPDATE teams SET created_by_user_id = NULL WHERE created_by_user_id = target_user_id;

    DELETE FROM auth_tokens   WHERE user_id = target_user_id;
    DELETE FROM auth_sessions WHERE user_id = target_user_id;
    DELETE FROM users WHERE id = target_user_id;

    -- The Supabase Auth identity is a separate object. Leaving it means the
    -- person signs in again and the signup trigger builds them a fresh empty
    -- account -- which looks like the delete silently failed.
    IF v_external IS NOT NULL THEN
        BEGIN
            EXECUTE 'DELETE FROM auth.users WHERE id = $1::uuid' USING v_external;
            v_auth_gone := TRUE;
        EXCEPTION WHEN others THEN
            -- Reported rather than fatal: the application data is already
            -- gone, and rolling that back because the auth schema is out of
            -- reach would leave the worse of the two states.
            v_auth_gone := FALSE;
        END;
    END IF;

    RETURN jsonb_build_object(
        'user_id', target_user_id,
        'email', v_email,
        'sessions_deleted', COALESCE(v_sessions_n, 0),
        'auth_identity_deleted', v_auth_gone
    );
END;
$$;

REVOKE ALL ON FUNCTION admin_delete_user(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_delete_user(BIGINT) TO authenticated;
