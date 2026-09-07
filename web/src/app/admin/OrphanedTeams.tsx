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
 * escape hatch; this is just its one small affordance in the admin view,
 * not a general team-editing feature.
 */
export default function OrphanedTeams({ teams }: { teams: OrphanedTeamRow[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<number, number | undefined>>({});

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
              <span className="text-xs text-muted">no active members left -- nobody to promote</span>
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
