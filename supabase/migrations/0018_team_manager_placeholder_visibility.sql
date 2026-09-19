-- Lets a team manager (or admin) see and reassign placeholder drivers --
-- and the sessions attributed to them -- that were created by any of
-- their team's other active members, not only ones they created
-- themselves.
--
-- 0017 scoped both the placeholder-visibility and the
-- reassign_temp_driver_sessions RPC to "whoever created it", on the
-- assumption that's normally the team manager anyway. In practice it
-- isn't always: any active member can upload on a teammate's behalf and
-- pick "+ Add a new driver...", and the manager -- who is the one
-- actually responsible for tidying up placeholders once a driver
-- registers for real -- had no way to see that placeholder existed at
-- all, let alone reassign its sessions.
--
-- `user_manages_team_of` is the one new primitive this needs: "is the
-- caller an active manager/admin of a team that `p_target_user_id`'s own
-- (claimed) driver profile is an active member of." A placeholder has no
-- team_memberships row of its own to test against -- it isn't a driver on
-- anyone's roster -- so authorisation has to run through its *creator*'s
-- team membership instead. SECURITY DEFINER for the same reason every
-- other team-lookup helper here is (0001's is_active_team_member /
-- shares_active_team_with, 0010's current_role_in_team): it reads
-- team_memberships and driver_profiles from inside a policy that itself
-- restricts driver_profiles, which would otherwise recurse through RLS.
CREATE OR REPLACE FUNCTION user_manages_team_of(p_target_user_id BIGINT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM team_memberships tm_target
        JOIN driver_profiles target_p ON target_p.id = tm_target.driver_profile_id
        WHERE target_p.user_id = p_target_user_id
          AND tm_target.status = 'active'
          AND current_role_in_team(tm_target.team_id) IN ('manager', 'admin')
    );
$$;

-- Adds one OR branch to 0001's driver_profiles_select: a manager/admin
-- can now also see an unclaimed/invited placeholder created by an active
-- member of a team they manage. Claimed profiles are unaffected -- they
-- were already visible to everyone via the first branch.
DROP POLICY IF EXISTS driver_profiles_select ON driver_profiles;
CREATE POLICY driver_profiles_select ON driver_profiles
    FOR SELECT USING (
        claim_status = 'claimed'
        OR user_id = current_app_user_id()
        OR created_by_user_id = current_app_user_id()
        OR (
            claim_status <> 'claimed'
            AND created_by_user_id IS NOT NULL
            AND user_manages_team_of(created_by_user_id)
        )
    );

-- Adds the matching branch to 0002's sessions_select: seeing that a
-- placeholder exists is not much use without being able to see what was
-- actually uploaded onto it. Deliberately keyed on the *placeholder's*
-- claim_status/creator, not on visibility/attribution_status the way the
-- public and team branches above it are -- an unclaimed profile has no
-- owner to have set a sharing preference at all, so those columns don't
-- apply here the way they do to a real driver's session.
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
            OR EXISTS (
                SELECT 1 FROM driver_profiles p
                WHERE p.id = sessions.driver_profile_id
                  AND p.claim_status <> 'claimed'
                  AND p.created_by_user_id IS NOT NULL
                  AND user_manages_team_of(p.created_by_user_id)
            )
        )
    );

-- Widens 0017's authorisation check: the placeholder's creator, or now
-- also a manager/admin of a team that creator actively belongs to.
-- Everything else -- the destination must already be claimed, the source
-- must not already be claimed -- is unchanged.
CREATE OR REPLACE FUNCTION reassign_temp_driver_sessions(p_from_profile_id BIGINT, p_to_profile_id BIGINT)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_from_claim_status TEXT;
    v_from_created_by BIGINT;
    v_to_claim_status TEXT;
    v_moved INTEGER;
BEGIN
    SELECT claim_status, created_by_user_id INTO v_from_claim_status, v_from_created_by
      FROM driver_profiles
     WHERE id = p_from_profile_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No such driver profile.' USING ERRCODE = '22023';
    END IF;
    IF v_from_claim_status = 'claimed' THEN
        RAISE EXCEPTION 'That profile is already a real, claimed driver -- nothing to reassign.'
            USING ERRCODE = '22023';
    END IF;
    IF v_from_created_by IS DISTINCT FROM current_app_user_id()
       AND NOT (v_from_created_by IS NOT NULL AND user_manages_team_of(v_from_created_by)) THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    SELECT claim_status INTO v_to_claim_status FROM driver_profiles WHERE id = p_to_profile_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No such driver profile.' USING ERRCODE = '22023';
    END IF;
    IF v_to_claim_status <> 'claimed' THEN
        RAISE EXCEPTION 'The target driver has not registered yet -- pick a real, claimed profile.'
            USING ERRCODE = '22023';
    END IF;

    UPDATE sessions SET driver_profile_id = p_to_profile_id WHERE driver_profile_id = p_from_profile_id;
    GET DIAGNOSTICS v_moved = ROW_COUNT;
    RETURN v_moved;
END;
$$;

REVOKE ALL ON FUNCTION reassign_temp_driver_sessions(BIGINT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reassign_temp_driver_sessions(BIGINT, BIGINT) TO authenticated;
