-- Team management: creating a team, requesting to join, a manager/admin
-- accepting or rejecting that request, promoting/demoting a role, removing
-- a member, and transferring the manager role.
--
-- 0002's own comment on `team_memberships_insert_request`/`_leave_own` said
-- "everything that changes someone else's standing ... stays server-side
-- for now." This migration is that server side. The retired Streamlit
-- prototype ran these as plain Python method calls
-- (`telemetry/accounts.py`'s `AccountLibrary`/`SupabaseAccountLibrary`) on a
-- superuser connection, trusting the UI to only show a manager their own
-- "Approve" button. The Next.js app has no such connection -- every query,
-- this one included, runs as the signed-in user under RLS -- so each action
-- below is a SECURITY DEFINER function that re-derives and checks the
-- caller's own authority from scratch, the same way `admin_user_overview`/
-- `admin_delete_user` (0008) already do for admin actions. There is no
-- shortcut through a plain RLS policy for most of these: promoting,
-- removing and transferring all depend on cross-row facts (the caller's own
-- role in this specific team, the target's current role) that a single
-- row's USING/WITH CHECK can't express without smuggling in the same
-- SECURITY DEFINER helper functions anyway.

-- ---------------------------------------------------------------------------
-- 1. Schema hardening: two invariants the original schema left to app-layer
--    discipline (a comment in 0001 says a portable partial-unique index
--    across SQLite and Postgres was awkward -- moot now that the SQLite
--    path is offline/local-only and Postgres is the only production
--    backend). Both are defence in depth: none of the RPCs below should
--    ever be able to violate these, but a direct-SQL mistake or a future
--    bug could.
-- ---------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS team_memberships_one_live_per_profile
    ON team_memberships (driver_profile_id)
    WHERE status IN ('pending', 'active');

CREATE UNIQUE INDEX IF NOT EXISTS team_one_active_manager
    ON team_memberships (team_id)
    WHERE role = 'manager' AND status = 'active';

-- ---------------------------------------------------------------------------
-- 2. Fixing `team_memberships_leave_own` (0002): its USING clause let the
--    team's own manager self-transition to 'left', orphaning the team --
--    the "must transfer first" rule only ever existed in the old Python
--    `leave_team` method, never in this policy. And its WITH CHECK pins
--    `status`/`driver_profile_id` but not which *columns* a self-leave PATCH
--    may touch, so nothing stopped a client also smuggling e.g. `role` into
--    the same request (inert today, since every authority check below
--    requires status='active' -- but the same "an UPDATE policy decides
--    which rows may be touched, never which columns" gap 0006/0007 already
--    closed elsewhere, so it's closed the same way here rather than relying
--    on inertness surviving future changes).
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS team_memberships_leave_own ON team_memberships;
CREATE POLICY team_memberships_leave_own ON team_memberships
    FOR UPDATE
    USING (
        driver_profile_id = current_app_profile_id()
        -- The manager cannot use this path at all -- team_transfer_manager
        -- below is the only way out, so a team is never left without an
        -- active manager.
        AND role <> 'manager'
    )
    WITH CHECK (
        driver_profile_id = current_app_profile_id()
        -- Leaving or withdrawing only -- never self-promotion to active.
        AND status IN ('left', 'rejected')
    );

REVOKE UPDATE ON team_memberships FROM anon, authenticated;
GRANT UPDATE (status, decided_at) ON team_memberships TO authenticated;

-- ---------------------------------------------------------------------------
-- 3. `current_role_in_team`: the caller's own active role on a given team,
--    or NULL if they're not an active member of it. SECURITY DEFINER for
--    the same reason `is_active_team_member` is (0001) -- it reads
--    `team_memberships` and is called from functions that mutate
--    `team_memberships`, which would otherwise recurse through RLS.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION current_role_in_team(check_team_id BIGINT)
RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT tm.role FROM team_memberships tm
    WHERE tm.team_id = check_team_id
      AND tm.driver_profile_id = current_app_profile_id()
      AND tm.status = 'active';
$$;

-- ---------------------------------------------------------------------------
-- 4. team_create: makes the caller a team's first member, atomically, so a
--    client can never create a team without also becoming its manager (and
--    never end up with a team but no membership row, or vice versa).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION team_create(p_name TEXT)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_profile_id BIGINT := current_app_profile_id();
    v_team_id BIGINT;
BEGIN
    IF v_profile_id IS NULL THEN
        RAISE EXCEPTION 'You need a driver profile before creating a team.' USING ERRCODE = '22023';
    END IF;
    IF p_name IS NULL OR length(trim(p_name)) = 0 THEN
        RAISE EXCEPTION 'Team name is required.' USING ERRCODE = '22023';
    END IF;
    -- Friendly pre-check; team_memberships_one_live_per_profile (above) is
    -- the hard backstop against a double-submit racing itself into two rows.
    IF EXISTS (
        SELECT 1 FROM team_memberships
         WHERE driver_profile_id = v_profile_id AND status IN ('pending', 'active')
    ) THEN
        RAISE EXCEPTION 'You are already on (or waiting on) a team -- leave it first.' USING ERRCODE = '22023';
    END IF;

    INSERT INTO teams (name, created_by_user_id, created_at)
    VALUES (trim(p_name), current_app_user_id(), now())
    RETURNING id INTO v_team_id;

    INSERT INTO team_memberships
        (team_id, driver_profile_id, role, status, requested_at, decided_at, decided_by_user_id)
    VALUES
        (v_team_id, v_profile_id, 'manager', 'active', now(), now(), current_app_user_id());

    RETURN v_team_id;
END;
$$;

REVOKE ALL ON FUNCTION team_create(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_create(TEXT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 5. team_resolve_join_request: a manager or admin accepts or rejects a
--    pending request. `FOR UPDATE` locks the target row for the rest of
--    this transaction, so two managers approving the same request
--    concurrently can't both apply -- the second one's final CAS below
--    finds the row already decided and raises instead of double-applying.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION team_resolve_join_request(p_membership_id BIGINT, p_accept BOOLEAN)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_team_id BIGINT;
BEGIN
    SELECT team_id INTO v_team_id
      FROM team_memberships
     WHERE id = p_membership_id AND status = 'pending'
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No pending request with that id.' USING ERRCODE = '22023';
    END IF;

    IF current_role_in_team(v_team_id) NOT IN ('manager', 'admin') THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    UPDATE team_memberships
       SET status = CASE WHEN p_accept THEN 'active' ELSE 'rejected' END,
           decided_at = now(),
           decided_by_user_id = current_app_user_id()
     WHERE id = p_membership_id AND status = 'pending';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'That request was already decided.' USING ERRCODE = '22023';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION team_resolve_join_request(BIGINT, BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_resolve_join_request(BIGINT, BOOLEAN) TO authenticated;

-- ---------------------------------------------------------------------------
-- 6. team_set_member_role: promote/demote between 'member' and 'admin'.
--    Manager-only (README rule: an admin cannot promote or demote anyone,
--    only the manager can) -- and refuses to touch a 'manager' row at all,
--    since changing the manager is a `team_transfer_manager` action, not a
--    role edit.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION team_set_member_role(p_membership_id BIGINT, p_new_role TEXT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_team_id BIGINT;
    v_target_role TEXT;
BEGIN
    IF p_new_role NOT IN ('member', 'admin') THEN
        RAISE EXCEPTION 'Use team_transfer_manager to change the manager, not team_set_member_role(%).', p_new_role
            USING ERRCODE = '22023';
    END IF;

    SELECT team_id, role INTO v_team_id, v_target_role
      FROM team_memberships
     WHERE id = p_membership_id AND status = 'active'
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No active member with that id.' USING ERRCODE = '22023';
    END IF;
    IF v_target_role = 'manager' THEN
        RAISE EXCEPTION 'The manager''s role can''t be changed directly -- transfer ownership first.'
            USING ERRCODE = '22023';
    END IF;

    IF current_role_in_team(v_team_id) <> 'manager' THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    UPDATE team_memberships SET role = p_new_role
     WHERE id = p_membership_id AND status = 'active' AND role <> 'manager';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'That member changed status before the update completed.' USING ERRCODE = '22023';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION team_set_member_role(BIGINT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_set_member_role(BIGINT, TEXT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 7. team_remove_member: a manager or admin revokes an active member's
--    access. Refuses to remove the manager (transfer first). README rule:
--    an admin cannot remove another admin -- only the manager can, so
--    admins can't act on each other.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION team_remove_member(p_membership_id BIGINT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_team_id BIGINT;
    v_target_role TEXT;
    v_caller_role TEXT;
BEGIN
    SELECT team_id, role INTO v_team_id, v_target_role
      FROM team_memberships
     WHERE id = p_membership_id AND status = 'active'
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No active member with that id.' USING ERRCODE = '22023';
    END IF;
    IF v_target_role = 'manager' THEN
        RAISE EXCEPTION 'The manager can''t be removed -- transfer ownership first.' USING ERRCODE = '22023';
    END IF;

    v_caller_role := current_role_in_team(v_team_id);
    IF v_caller_role NOT IN ('manager', 'admin') THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;
    IF v_target_role = 'admin' AND v_caller_role <> 'manager' THEN
        RAISE EXCEPTION 'Only the manager can remove an admin.' USING ERRCODE = '42501';
    END IF;

    UPDATE team_memberships
       SET status = 'removed', decided_at = now(), decided_by_user_id = current_app_user_id()
     WHERE id = p_membership_id AND status = 'active' AND role <> 'manager';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'That member changed status before the update completed.' USING ERRCODE = '22023';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION team_remove_member(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_remove_member(BIGINT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 8. team_transfer_manager: the only way the manager role ever moves.
--    Caller must currently be the team's active manager. Both rows (the
--    caller's own and the target's) are locked together in one statement,
--    in ascending-id order, so a concurrent call touching the same two rows
--    can't deadlock against this one by acquiring them in the opposite
--    order; both are re-checked as still 'active' after the lock, so a
--    target removed mid-transfer aborts cleanly instead of promoting a
--    removed row.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION team_transfer_manager(p_new_manager_membership_id BIGINT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_target_team BIGINT;
    v_caller_id BIGINT;
BEGIN
    SELECT team_id INTO v_target_team FROM team_memberships WHERE id = p_new_manager_membership_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No such membership.' USING ERRCODE = '22023';
    END IF;

    SELECT id INTO v_caller_id
      FROM team_memberships
     WHERE team_id = v_target_team
       AND driver_profile_id = current_app_profile_id()
       AND role = 'manager' AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;
    IF v_caller_id = p_new_manager_membership_id THEN
        RAISE EXCEPTION 'Already the manager.' USING ERRCODE = '22023';
    END IF;

    PERFORM 1 FROM team_memberships
     WHERE id IN (v_caller_id, p_new_manager_membership_id)
     ORDER BY id
       FOR UPDATE;
    IF (SELECT status FROM team_memberships WHERE id = p_new_manager_membership_id) <> 'active'
       OR (SELECT status FROM team_memberships WHERE id = v_caller_id) <> 'active' THEN
        RAISE EXCEPTION 'A membership changed status before the transfer completed.' USING ERRCODE = '22023';
    END IF;

    UPDATE team_memberships SET role = 'admin' WHERE id = v_caller_id;
    UPDATE team_memberships SET role = 'manager' WHERE id = p_new_manager_membership_id;
END;
$$;

REVOKE ALL ON FUNCTION team_transfer_manager(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION team_transfer_manager(BIGINT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 9. admin_team_reassign_manager: a real gap `admin_delete_user` (0008)
--    opens once the functions above exist. That function deletes a
--    departing user's `team_memberships` rows unconditionally, including an
--    active manager's -- harmless before this migration (nothing let anyone
--    act on a team's membership anyway), but `team_transfer_manager` above
--    requires calling *as* the active manager, so deleting a team's
--    manager's account would otherwise leave that team permanently
--    unmanageable: no one left who can promote a replacement. This is the
--    escape hatch, gated the same way every other admin action in 0008 is.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION admin_team_reassign_manager(p_new_manager_membership_id BIGINT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_target_team BIGINT;
    v_old_manager_id BIGINT;
BEGIN
    IF NOT is_app_admin() THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    SELECT team_id INTO v_target_team FROM team_memberships WHERE id = p_new_manager_membership_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No such membership.' USING ERRCODE = '22023';
    END IF;

    -- There may be no current manager at all (that's exactly the case this
    -- exists for), so this is best-effort, not required to find one.
    SELECT id INTO v_old_manager_id
      FROM team_memberships
     WHERE team_id = v_target_team AND role = 'manager' AND status = 'active';

    IF v_old_manager_id IS NOT NULL THEN
        PERFORM 1 FROM team_memberships
         WHERE id IN (v_old_manager_id, p_new_manager_membership_id)
         ORDER BY id
           FOR UPDATE;
    ELSE
        PERFORM 1 FROM team_memberships WHERE id = p_new_manager_membership_id FOR UPDATE;
    END IF;

    IF (SELECT status FROM team_memberships WHERE id = p_new_manager_membership_id) <> 'active' THEN
        RAISE EXCEPTION 'That membership is no longer active.' USING ERRCODE = '22023';
    END IF;

    IF v_old_manager_id IS NOT NULL THEN
        UPDATE team_memberships SET role = 'admin' WHERE id = v_old_manager_id;
    END IF;
    UPDATE team_memberships SET role = 'manager' WHERE id = p_new_manager_membership_id;
END;
$$;

REVOKE ALL ON FUNCTION admin_team_reassign_manager(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_team_reassign_manager(BIGINT) TO authenticated;

-- ---------------------------------------------------------------------------
-- 10. admin_list_orphaned_teams: what the admin page needs to *find* a team
--     to reassign. `team_memberships_select` only lets a client see a
--     team's rows if they are themselves an active member of it (0001), so
--     an admin's own RLS-scoped queries cannot tell which teams across the
--     whole app have no active manager, or who their active members even
--     are -- being `is_admin` is an application-level flag, not team
--     membership. Returns each active member alongside the team so
--     `admin_team_reassign_manager` above has a membership id to call with,
--     without a second round-trip RPC.
-- ---------------------------------------------------------------------------

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
                          jsonb_build_object('membership_id', tm.id, 'display_name', p.display_name, 'role', tm.role)
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
