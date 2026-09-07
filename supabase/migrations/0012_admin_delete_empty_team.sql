-- admin_delete_empty_team: the cleanup step admin_list_orphaned_teams'
-- "no active members left" case had no action for.
--
-- A team can end up with zero active members entirely legitimately -- most
-- commonly every member's account is removed via admin_delete_user (0008),
-- which hard-deletes their team_memberships rows along with everything else
-- of theirs, leaving the bare `teams` row behind with nothing to reassign
-- (admin_team_reassign_manager needs an existing active membership to
-- promote; there is none left). There is no data left to salvage in that
-- case -- driver_profiles and sessions went with the deleted accounts too --
-- so the only useful admin action is deleting the empty shell.
--
-- Deliberately narrower than "delete any orphaned team": an orphaned team
-- can still have active admins/members, just no manager -- that case has
-- admin_team_reassign_manager, and deleting it out from under real members
-- would be a much bigger, more surprising action than this button implies.
-- So this refuses outright if any membership row is still 'active',
-- regardless of role.

CREATE OR REPLACE FUNCTION admin_delete_empty_team(p_team_id BIGINT)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT is_app_admin() THEN
        RAISE EXCEPTION 'Not authorised' USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM teams WHERE id = p_team_id) THEN
        RAISE EXCEPTION 'No such team.' USING ERRCODE = '22023';
    END IF;

    -- Lock every membership row for this team before deciding: a join
    -- request resolving to 'active' concurrently must not race past this
    -- check.
    PERFORM 1 FROM team_memberships WHERE team_id = p_team_id FOR UPDATE;

    IF EXISTS (SELECT 1 FROM team_memberships WHERE team_id = p_team_id AND status = 'active') THEN
        RAISE EXCEPTION 'This team still has an active member -- reassign a manager instead of deleting it.'
            USING ERRCODE = '22023';
    END IF;

    -- Whatever non-active rows remain (a rejected request, someone who left
    -- before the team went quiet) are just history for a team that is about
    -- to stop existing -- team_memberships.team_id has no ON DELETE clause,
    -- so leaving them would block the delete with a foreign-key error.
    DELETE FROM team_memberships WHERE team_id = p_team_id;
    DELETE FROM teams WHERE id = p_team_id;
END;
$$;

REVOKE ALL ON FUNCTION admin_delete_empty_team(BIGINT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION admin_delete_empty_team(BIGINT) TO authenticated;
