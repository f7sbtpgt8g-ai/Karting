-- Temp/placeholder driver profiles, created straight from the "assign a
-- driver" picker on Upload (see web/src/app/upload/UnassignedSessions.tsx),
-- and a way to move a temp profile's sessions onto a real one once its
-- driver actually registers.
--
-- Two gaps this closes:
--
-- 1. There was no client-facing way to create a `driver_profiles` row at
--    all -- every existing row came from a real signup (0004/0007/0013's
--    triggers) or `telemetry.accounts.AccountLibrary.create_unclaimed_profile`
--    on a service-role connection (the Streamlit-era admin flow, and
--    unigo_sync's own `core.auth_session.create_driver`). Someone
--    attributing an upload to a teammate who hasn't signed up yet had
--    nothing to pick from the dropdown for them.
--
-- 2. A session's `driver_profile_id` points at a `driver_profiles` row
--    directly -- when that row is later claimed (linked to a real
--    `user_id`), every session already pointing at it becomes that
--    person's automatically, no data migration needed. But this app has
--    no claim-token/invite flow yet (unlike the old Streamlit app's
--    `claim_profile_by_token`), so in practice a teammate who registers
--    today gets a brand new profile from the signup trigger, not a claim
--    on the placeholder. `reassign_temp_driver_sessions` is the bridge:
--    whoever created the placeholder can move its sessions onto the new,
--    real profile by hand once that happens.
-- ---------------------------------------------------------------------------

-- Anyone can create a placeholder for someone else -- but only ever
-- unclaimed, owned by nobody, and credited to themselves as creator. There
-- is deliberately no INSERT path for a claimed profile or one attributed to
-- another user_id; that only ever happens through a real signup.
DROP POLICY IF EXISTS driver_profiles_insert_placeholder ON driver_profiles;
CREATE POLICY driver_profiles_insert_placeholder ON driver_profiles
    FOR INSERT
    WITH CHECK (
        created_by_user_id = current_app_user_id()
        AND claim_status = 'unclaimed'
        AND user_id IS NULL
    );

-- ---------------------------------------------------------------------------
-- reassign_temp_driver_sessions: move every session currently attributed to
-- an unclaimed placeholder profile onto a real, claimed one.
--
-- Deliberately a SECURITY DEFINER function rather than a wider
-- `sessions_update_own` USING/WITH CHECK clause: RLS's WITH CHECK is
-- evaluated against the *new* row, and the new row's driver_profile_id is
-- the destination profile -- which the caller did not create and does not
-- own -- so no USING/WITH CHECK pair keyed on "who created the source
-- profile" can also hold after the very update that changes it away from
-- that profile. A function re-checking authorisation itself sidesteps that
-- rather than fighting it -- the same reasoning 0010's team_* functions
-- already follow for actions RLS alone can't express.
--
-- Authorised for whoever created the placeholder (in practice, the manager
-- who uploaded on the teammate's behalf and picked "+ Add a new driver"),
-- not "any team manager" generally -- there is no team_memberships row for
-- an unclaimed profile to test against, so profile authorship is the only
-- ownership signal available. p_to_profile_id must already be a claimed,
-- real profile: the whole point is "attribute this to the driver's actual
-- account", not a second placeholder-to-placeholder shuffle.
-- ---------------------------------------------------------------------------

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
    IF v_from_created_by IS DISTINCT FROM current_app_user_id() THEN
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
