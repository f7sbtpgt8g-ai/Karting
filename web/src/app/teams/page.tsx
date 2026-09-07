import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import TeamsClient, { type PendingRequestRow, type RosterRow, type TeamOption } from "./TeamsClient";
import type { TeamRole, TeamMembershipStatus } from "@/lib/teams";

export const dynamic = "force-dynamic";

/**
 * Teams: create one, request to join one, and -- once active -- see the
 * roster and, if you manage or admin it, the pending-requests inbox and the
 * promote/remove/transfer controls.
 *
 * Every write this page's client component makes either goes through RLS
 * directly (requesting to join, leaving) or through one of the
 * `team_*`/`admin_team_reassign_manager` SECURITY DEFINER RPCs in
 * `supabase/migrations/0010_teams_management.sql`, which each re-derive and
 * check the caller's own authority from scratch -- this page's job is only
 * to read the caller's current standing and render the right view, mirroring
 * `HomePage`'s own inline driver_profile/team_memberships resolution rather
 * than a shared helper (this codebase's own idiom favours an explicit local
 * query over premature sharing).
 */
export default async function TeamsPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  const { data: myProfile } = await supabase
    .from("driver_profiles")
    .select("id")
    .eq("user_id", appUser.id)
    .maybeSingle();

  type RawMembership = {
    id: number;
    role: TeamRole;
    status: TeamMembershipStatus;
    team_id: number;
    requested_at: string | null;
    teams: { name: string } | null;
  };

  let membership: {
    id: number;
    role: TeamRole;
    status: TeamMembershipStatus;
    teamId: number;
    teamName: string;
  } | null = null;
  let roster: RosterRow[] = [];
  let pendingRequests: PendingRequestRow[] = [];
  let allTeams: TeamOption[] = [];

  if (myProfile) {
    // The most recent row, whatever its status -- a driver can have many
    // historical (left/rejected/removed) rows but at most one live
    // (pending/active) one at a time (team_memberships_one_live_per_profile,
    // 0010), so "most recent" and "current" agree.
    const { data: raw } = await supabase
      .from("team_memberships")
      .select("id, role, status, team_id, requested_at, teams(name)")
      .eq("driver_profile_id", myProfile.id)
      .order("id", { ascending: false })
      .limit(1)
      .maybeSingle()
      .returns<RawMembership | null>();

    if (raw && (raw.status === "pending" || raw.status === "active")) {
      membership = {
        id: raw.id,
        role: raw.role,
        status: raw.status,
        teamId: raw.team_id,
        teamName: raw.teams?.name ?? "Unknown team",
      };
    }

    if (membership?.status === "active") {
      type RawRoster = {
        id: number;
        role: TeamRole;
        driver_profile_id: number;
        driver_profiles: { display_name: string } | null;
      };
      const { data: rosterRows } = await supabase
        .from("team_memberships")
        .select("id, role, driver_profile_id, driver_profiles(display_name)")
        .eq("team_id", membership.teamId)
        .eq("status", "active")
        .returns<RawRoster[]>();
      roster = (rosterRows ?? []).map((r) => ({
        id: r.id,
        role: r.role,
        driverProfileId: r.driver_profile_id,
        displayName: r.driver_profiles?.display_name ?? "Unknown driver",
      }));

      if (membership.role === "manager" || membership.role === "admin") {
        type RawPending = {
          id: number;
          requested_at: string | null;
          driver_profile_id: number;
          driver_profiles: { display_name: string } | null;
        };
        const { data: pendingRows } = await supabase
          .from("team_memberships")
          .select("id, requested_at, driver_profile_id, driver_profiles(display_name)")
          .eq("team_id", membership.teamId)
          .eq("status", "pending")
          .order("requested_at", { ascending: true })
          .returns<RawPending[]>();
        pendingRequests = (pendingRows ?? []).map((r) => ({
          id: r.id,
          driverProfileId: r.driver_profile_id,
          displayName: r.driver_profiles?.display_name ?? "Unknown driver",
          requestedAt: r.requested_at,
        }));
      }
    }
  }

  if (!membership) {
    const { data: teamRows } = await supabase
      .from("teams")
      .select("id, name")
      .order("name")
      .returns<TeamOption[]>();
    allTeams = teamRows ?? [];
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-8">
      <AppHeader email={appUser.email} current="/teams" isAdmin={appUser?.is_admin} />
      <h1 className="mb-1 text-lg font-semibold">Team</h1>
      <p className="mb-6 text-sm text-muted">
        A team is a second, narrower sharing circle: a session marked &ldquo;Team&rdquo; visibility
        is seen by fellow active members, without going to the public leaderboards or shared laps.
      </p>
      <TeamsClient
        myProfileId={myProfile?.id ?? null}
        membership={membership}
        roster={roster}
        pendingRequests={pendingRequests}
        allTeams={allTeams}
      />
    </main>
  );
}
