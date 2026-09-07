"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { TEAM_ROLE_LABELS, type TeamRole } from "@/lib/teams";

export type OrphanedTeamRow = {
  id: number;
  name: string;
  members: Array<{ membership_id: number; display_name: string; role: TeamRole }>;
};

/**
 * A team with no active manager -- possible once `admin_delete_user`
 * removes a manager's account, since `team_transfer_manager` requires
 * calling *as* the active manager and there is otherwise no one left who
 * can promote a replacement. `admin_team_reassign_manager` (0010) is the
 * escape hatch when other active members remain to promote.
 *
 * If `admin_delete_user` took every member with it, there is nothing left
 * to reassign -- driver_profiles and sessions went with the deleted
 * accounts too, so the team is just an empty name. `admin_delete_empty_team`
 * (0012) cleans that up; it refuses outright if the team still has any
 * active member, which is what keeps this a narrow "delete the empty
 * shell" button rather than a general team-editing feature.
 */
export default function OrphanedTeams({ teams }: { teams: OrphanedTeamRow[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<number, number | undefined>>({});
  const [confirmingDelete, setConfirmingDelete] = useState<number | null>(null);

  if (teams.length === 0) return null;

  async function reassign(teamId: number) {
    const membershipId = picked[teamId];
    if (!membershipId) return;
    setBusy(true);
    setError(null);
    const { error: rpcError } = await createClient().rpc("admin_team_reassign_manager", {
      p_new_manager_membership_id: membershipId,
    });
    setBusy(false);
    if (rpcError) {
      setError(rpcError.message);
      return;
    }
    router.refresh();
  }

  async function deleteEmptyTeam(teamId: number) {
    setBusy(true);
    setError(null);
    const { error: rpcError } = await createClient().rpc("admin_delete_empty_team", {
      p_team_id: teamId,
    });
    setBusy(false);
    setConfirmingDelete(null);
    if (rpcError) {
      setError(rpcError.message);
      return;
    }
    router.refresh();
  }

  return (
    <div className="mb-6 rounded border border-theoretical/40 bg-theoretical/10 p-4">
      <h2 className="mb-1 text-sm font-semibold text-theoretical">
        {teams.length} team{teams.length === 1 ? "" : "s"} with no active manager
      </h2>
      <p className="mb-3 text-xs text-muted">
        Nobody can approve join requests, promote/remove members, or transfer ownership on these
        until one is reassigned.
      </p>
      {error && <p className="mb-3 text-xs text-loss">{error}</p>}
      <div className="space-y-2">
        {teams.map((team) => (
          <div key={team.id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-semibold">{team.name}</span>
            {team.members.length === 0 ? (
              <>
                <span className="text-xs text-muted">no active members left -- nothing to reassign</span>
                {confirmingDelete === team.id ? (
                  <>
                    <span className="text-xs text-loss">Delete this team permanently?</span>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => deleteEmptyTeam(team.id)}
                      className="rounded bg-loss px-3 py-1 text-xs font-semibold text-canvas disabled:opacity-40"
                    >
                      Confirm delete
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => setConfirmingDelete(null)}
                      className="rounded border border-hairline px-3 py-1 text-xs text-muted"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setConfirmingDelete(team.id)}
                    className="rounded border border-loss/40 px-3 py-1 text-xs font-semibold text-loss disabled:opacity-40"
                  >
                    Delete team
                  </button>
                )}
              </>
            ) : (
              <>
                <select
                  value={picked[team.id] ?? ""}
                  onChange={(e) =>
                    setPicked((prev) => ({ ...prev, [team.id]: Number(e.target.value) || undefined }))
                  }
                  className="rounded border border-hairline bg-surface px-2 py-1 text-xs"
                >
                  <option value="">Choose a new manager…</option>
                  {team.members.map((m) => (
                    <option key={m.membership_id} value={m.membership_id}>
                      {m.display_name} ({TEAM_ROLE_LABELS[m.role]})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy || !picked[team.id]}
                  onClick={() => reassign(team.id)}
                  className="rounded bg-theoretical px-3 py-1 text-xs font-semibold text-canvas disabled:opacity-40"
                >
                  Reassign
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
