"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { TEAM_ROLE_LABELS, canActOnMember, compareTeamRole, type TeamRole, type TeamMembershipStatus } from "@/lib/teams";
import { DriverName } from "@/components/CountryFlag";

export type RosterRow = {
  id: number;
  role: TeamRole;
  driverProfileId: number;
  displayName: string;
  country: string | null;
};
export type PendingRequestRow = {
  id: number;
  driverProfileId: number;
  displayName: string;
  requestedAt: string | null;
  country: string | null;
};
export type TeamOption = { id: number; name: string };

type Membership = {
  id: number;
  role: TeamRole;
  status: TeamMembershipStatus;
  teamId: number;
  teamName: string;
};

/**
 * All the write paths a signed-in driver has for their own team standing.
 * Requesting to join and leaving/withdrawing go straight through RLS
 * (`team_memberships_insert_request`/`team_memberships_leave_own`, 0002 +
 * 0010) -- everything that acts on *someone else's* row calls one of the
 * SECURITY DEFINER RPCs in 0010, which is what actually decides whether the
 * caller may do it; `canActOnMember` here only decides what to render, not
 * what's allowed.
 */
export default function TeamsClient({
  myProfileId,
  membership,
  roster,
  pendingRequests,
  allTeams,
}: {
  myProfileId: number | null;
  membership: Membership | null;
  roster: RosterRow[];
  pendingRequests: PendingRequestRow[];
  allTeams: TeamOption[];
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [newTeamName, setNewTeamName] = useState("");
  const [confirmingRemove, setConfirmingRemove] = useState<number | null>(null);
  const [confirmingTransfer, setConfirmingTransfer] = useState<number | null>(null);

  const filteredTeams = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return allTeams;
    return allTeams.filter((t) => t.name.toLowerCase().includes(q));
  }, [allTeams, search]);

  const sortedRoster = useMemo(
    () => [...roster].sort((a, b) => compareTeamRole(a.role, b.role) || a.displayName.localeCompare(b.displayName)),
    [roster],
  );

  async function run(action: () => Promise<{ error: { message: string } | null }>) {
    setBusy(true);
    setError(null);
    const { error: actionError } = await action();
    setBusy(false);
    if (actionError) {
      setError(actionError.message);
      return false;
    }
    router.refresh();
    return true;
  }

  async function requestToJoin(teamId: number) {
    if (!myProfileId) return;
    await run(async () => {
      const { error: insertError } = await createClient()
        .from("team_memberships")
        .insert({
          team_id: teamId,
          driver_profile_id: myProfileId,
          role: "member",
          status: "pending",
          requested_at: new Date().toISOString(),
        });
      return { error: insertError };
    });
  }

  async function createTeam(event: React.FormEvent) {
    event.preventDefault();
    if (!newTeamName.trim()) return;
    await run(async () => {
      const { error: rpcError } = await createClient().rpc("team_create", { p_name: newTeamName.trim() });
      return { error: rpcError };
    });
  }

  async function withdrawOrLeave() {
    if (!membership) return;
    const nextStatus = membership.status === "pending" ? "rejected" : "left";
    await run(async () => {
      const { error: updateError } = await createClient()
        .from("team_memberships")
        .update({ status: nextStatus })
        .eq("id", membership.id);
      return { error: updateError };
    });
  }

  async function resolveRequest(membershipId: number, accept: boolean) {
    await run(async () => {
      const { error: rpcError } = await createClient().rpc("team_resolve_join_request", {
        p_membership_id: membershipId,
        p_accept: accept,
      });
      return { error: rpcError };
    });
  }

  async function setRole(membershipId: number, newRole: "member" | "admin") {
    await run(async () => {
      const { error: rpcError } = await createClient().rpc("team_set_member_role", {
        p_membership_id: membershipId,
        p_new_role: newRole,
      });
      return { error: rpcError };
    });
  }

  async function removeMember(membershipId: number) {
    const ok = await run(async () => {
      const { error: rpcError } = await createClient().rpc("team_remove_member", { p_membership_id: membershipId });
      return { error: rpcError };
    });
    if (ok) setConfirmingRemove(null);
  }

  async function transferManager(membershipId: number) {
    const ok = await run(async () => {
      const { error: rpcError } = await createClient().rpc("team_transfer_manager", {
        p_new_manager_membership_id: membershipId,
      });
      return { error: rpcError };
    });
    if (ok) setConfirmingTransfer(null);
  }

  const errorBanner = error && (
    <p className="mb-4 rounded border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss" role="alert">
      {error}
    </p>
  );

  // ---------------------------------------------------------- not on a team
  if (!membership) {
    return (
      <div>
        {errorBanner}

        <form onSubmit={createTeam} className="mb-8 rounded border border-hairline bg-surface p-4">
          <label className="label mb-1 block" htmlFor="team-name">
            Create a team
          </label>
          <div className="flex gap-2">
            <input
              id="team-name"
              value={newTeamName}
              onChange={(e) => setNewTeamName(e.target.value)}
              placeholder="Team name"
              className="w-full max-w-sm rounded border border-hairline bg-canvas px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button
              type="submit"
              disabled={busy || !newTeamName.trim()}
              className="shrink-0 rounded bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
            >
              Create
            </button>
          </div>
          <p className="mt-2 text-xs text-muted">You become its manager immediately.</p>
        </form>

        <div>
          <label className="label mb-1 block" htmlFor="team-search">
            Or join an existing team
          </label>
          <input
            id="team-search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search teams…"
            className="mb-3 w-full max-w-sm rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
            {filteredTeams.length === 0 && (
              <p className="px-4 py-3 text-sm text-muted">No teams found.</p>
            )}
            {filteredTeams.map((team) => (
              <div key={team.id} className="flex items-center justify-between px-4 py-3">
                <span className="text-sm">{team.name}</span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => requestToJoin(team.id)}
                  className="rounded border border-hairline px-3 py-1.5 text-xs font-semibold hover:border-accent disabled:opacity-40"
                >
                  Request to join
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------- pending
  if (membership.status === "pending") {
    return (
      <div>
        {errorBanner}
        <div className="rounded border border-hairline bg-surface p-6">
          <p className="mb-4 text-sm">
            Your request to join <strong>{membership.teamName}</strong> is awaiting approval.
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={withdrawOrLeave}
            className="rounded border border-hairline px-4 py-2 text-sm font-semibold hover:border-loss disabled:opacity-40"
          >
            Withdraw request
          </button>
        </div>
      </div>
    );
  }

  // ----------------------------------------------------------- active member
  const canManage = membership.role === "manager" || membership.role === "admin";

  return (
    <div>
      {errorBanner}

      <div className="mb-6 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold">{membership.teamName}</h2>
          <p className="text-xs text-muted">Your role: {TEAM_ROLE_LABELS[membership.role]}</p>
        </div>
        {membership.role !== "manager" && (
          <button
            type="button"
            disabled={busy}
            onClick={withdrawOrLeave}
            className="rounded border border-hairline px-3 py-1.5 text-xs font-semibold hover:border-loss disabled:opacity-40"
          >
            Leave team
          </button>
        )}
      </div>

      {canManage && pendingRequests.length > 0 && (
        <div className="mb-6">
          <h3 className="label mb-2">Pending requests</h3>
          <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
            {pendingRequests.map((req) => (
              <div key={req.id} className="flex items-center justify-between px-4 py-3">
                <span className="text-sm">
                  <DriverName name={req.displayName} country={req.country} />
                </span>
                <div className="flex gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => resolveRequest(req.id, true)}
                    className="rounded bg-gain px-3 py-1.5 text-xs font-semibold text-canvas disabled:opacity-40"
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => resolveRequest(req.id, false)}
                    className="rounded border border-hairline px-3 py-1.5 text-xs font-semibold hover:border-loss disabled:opacity-40"
                  >
                    Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div>
        <h3 className="label mb-2">Roster</h3>
        <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
          {sortedRoster.map((member) => {
            const isSelf = member.driverProfileId === myProfileId;
            const showManage = canManage && !isSelf && canActOnMember(membership.role, member.role);
            const managerCanPromote = membership.role === "manager" && !isSelf && member.role !== "manager";
            return (
              <div key={member.id} className="px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm">
                    <DriverName name={member.displayName} country={member.country} />{" "}
                    {isSelf && <span className="text-xs text-muted">(you)</span>}
                  </span>
                  <span className="text-xs text-muted">{TEAM_ROLE_LABELS[member.role]}</span>
                </div>
                {(showManage || managerCanPromote) && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {managerCanPromote && member.role === "member" && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setRole(member.id, "admin")}
                        className="rounded border border-hairline px-2.5 py-1 text-[11px] hover:border-accent disabled:opacity-40"
                      >
                        Make admin
                      </button>
                    )}
                    {managerCanPromote && member.role === "admin" && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setRole(member.id, "member")}
                        className="rounded border border-hairline px-2.5 py-1 text-[11px] hover:border-accent disabled:opacity-40"
                      >
                        Make plain member
                      </button>
                    )}
                    {managerCanPromote && confirmingTransfer !== member.id && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setConfirmingTransfer(member.id)}
                        className="rounded border border-hairline px-2.5 py-1 text-[11px] hover:border-theoretical disabled:opacity-40"
                      >
                        Make manager
                      </button>
                    )}
                    {confirmingTransfer === member.id && (
                      <span className="flex items-center gap-2 text-[11px]">
                        Hand over ownership to {member.displayName}?
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => transferManager(member.id)}
                          className="rounded bg-theoretical px-2.5 py-1 font-semibold text-canvas disabled:opacity-40"
                        >
                          Confirm
                        </button>
                        <button type="button" onClick={() => setConfirmingTransfer(null)} className="underline">
                          Cancel
                        </button>
                      </span>
                    )}
                    {showManage && confirmingRemove !== member.id && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setConfirmingRemove(member.id)}
                        className="rounded border border-hairline px-2.5 py-1 text-[11px] hover:border-loss disabled:opacity-40"
                      >
                        Remove
                      </button>
                    )}
                    {confirmingRemove === member.id && (
                      <span className="flex items-center gap-2 text-[11px]">
                        Remove {member.displayName}?
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => removeMember(member.id)}
                          className="rounded bg-loss px-2.5 py-1 font-semibold text-white disabled:opacity-40"
                        >
                          Confirm
                        </button>
                        <button type="button" onClick={() => setConfirmingRemove(null)} className="underline">
                          Cancel
                        </button>
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
