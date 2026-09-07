/**
 * Team roles and membership status.
 *
 * Kept identical to `telemetry/accounts.py`'s `TEAM_ROLE_*`/
 * `TEAM_MEMBERSHIP_*` constants and the values compared against in
 * `supabase/migrations/0002_rls_hardening.sql` /
 * `0010_teams_management.sql`, so a value written from here reads back
 * correctly everywhere else -- and matches exactly, since the RPCs raise on
 * anything else (`team_set_member_role` only accepts 'member'/'admin', for
 * instance).
 */
export const TEAM_ROLES = ["manager", "admin", "member"] as const;
export type TeamRole = (typeof TEAM_ROLES)[number];

export const TEAM_ROLE_LABELS: Record<TeamRole, string> = {
  manager: "Manager",
  admin: "Admin",
  member: "Member",
};

export type TeamMembershipStatus = "pending" | "active" | "rejected" | "left" | "removed";

/**
 * Manager first, then admin, then member -- the order
 * `AccountLibrary.team_roster` (telemetry/accounts.py) already sorts a
 * roster in, so a roster rendered here reads the same way as it would
 * anywhere else this data is shown.
 */
const TEAM_ROLE_ORDER: Record<TeamRole, number> = { manager: 0, admin: 1, member: 2 };

export function compareTeamRole(a: string, b: string): number {
  const orderOf = (role: string) => TEAM_ROLE_ORDER[role as TeamRole] ?? 99;
  return orderOf(a) - orderOf(b);
}

/**
 * Whether `viewerRole` may accept/reject a join request, or remove
 * `targetRole`. Mirrors the authority checks inside
 * `team_resolve_join_request`/`team_remove_member` (0010) -- kept here too
 * so the UI can grey out or hide a control a call would just reject anyway,
 * rather than showing it and surfacing the RPC's error after the fact. The
 * RPC is still what actually enforces this; this function is a convenience,
 * not a second copy of the security boundary.
 */
export function canActOnMember(viewerRole: TeamRole, targetRole: TeamRole): boolean {
  if (viewerRole !== "manager" && viewerRole !== "admin") return false;
  if (targetRole === "manager") return false;
  if (targetRole === "admin" && viewerRole !== "manager") return false;
  return true;
}
