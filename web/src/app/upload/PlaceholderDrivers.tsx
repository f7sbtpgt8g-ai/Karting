"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export type PlaceholderProfile = { id: number; display_name: string };
export type ClaimedProfile = { id: number; display_name: string; claim_status: string };

/**
 * Placeholders the signed-in account created (via "+ Add a new driver..."
 * in UnassignedSessions, or `unigo_sync`'s own `create_driver`) that are
 * still unclaimed -- with a picker to move all of a placeholder's sessions
 * onto a real, registered profile once that teammate actually signs up.
 *
 * Calls `reassign_temp_driver_sessions` (0017) rather than updating
 * `sessions.driver_profile_id` directly: that RPC is the only thing that
 * can move a session the caller did not themselves upload (RLS's
 * `sessions_update_own` only ever grants the uploader, or a claimed
 * profile's own owner, neither of which the placeholder's creator
 * necessarily is) -- see the migration for why this needs a
 * SECURITY DEFINER function rather than a wider RLS policy.
 */
export default function PlaceholderDrivers({
  placeholders,
  sessionCounts,
  realProfiles,
}: {
  placeholders: PlaceholderProfile[];
  sessionCounts: Record<number, number>;
  realProfiles: ClaimedProfile[];
}) {
  const router = useRouter();
  const [counts, setCounts] = useState(sessionCounts);
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [justMoved, setJustMoved] = useState<Record<number, number>>({});

  // Kept visible through a successful reassign (count just dropped to 0)
  // so "Moved N sessions" has a row to show up in, rather than the row
  // vanishing the instant the count that gates it changes.
  const withSessions = placeholders.filter((p) => (counts[p.id] ?? 0) > 0 || justMoved[p.id] !== undefined);
  if (withSessions.length === 0) return null;

  async function reassign(placeholderId: number) {
    const targetId = picked[placeholderId];
    if (!targetId) return;
    setBusy(placeholderId);
    setError(null);
    const { data, error: rpcError } = await createClient().rpc("reassign_temp_driver_sessions", {
      p_from_profile_id: placeholderId,
      p_to_profile_id: Number(targetId),
    });
    setBusy(null);
    if (rpcError) {
      setError(rpcError.message);
      return;
    }
    setJustMoved((current) => ({ ...current, [placeholderId]: (data as number) ?? 0 }));
    setCounts((current) => ({ ...current, [placeholderId]: 0 }));
    router.refresh();
  }

  return (
    <section className="mb-10 rounded border border-hairline bg-raised p-4">
      <h2 className="mb-1 text-sm font-bold">Placeholder drivers you added</h2>
      <p className="mb-3 text-xs text-muted">
        Once one of these registers a real account, move their sessions onto it here.
      </p>

      {error && (
        <p className="mb-3 text-sm text-loss" role="alert">
          {error}
        </p>
      )}

      <div className="space-y-2">
        {withSessions.map((placeholder) => {
          const count = counts[placeholder.id] ?? 0;
          const moved = justMoved[placeholder.id];
          return (
            <div
              key={placeholder.id}
              className="flex flex-wrap items-center gap-2 rounded border border-hairline bg-surface px-3 py-2 text-sm"
            >
              <span className="font-semibold">{placeholder.display_name}</span>
              {count > 0 ? (
                <>
                  <span className="text-xs text-muted">
                    {count} session{count === 1 ? "" : "s"} attributed to this placeholder
                  </span>
                  {realProfiles.length === 0 ? (
                    <span className="ml-auto text-xs text-muted">
                      No registered drivers to reassign to yet.
                    </span>
                  ) : (
                    <>
                      <select
                        value={picked[placeholder.id] ?? ""}
                        disabled={busy === placeholder.id}
                        onChange={(e) =>
                          setPicked((current) => ({ ...current, [placeholder.id]: e.target.value }))
                        }
                        className="ml-auto rounded border border-hairline bg-canvas px-2 py-1 text-xs disabled:opacity-60"
                      >
                        <option value="">Now really is...</option>
                        {realProfiles.map((profile) => (
                          <option key={profile.id} value={profile.id}>
                            {profile.display_name}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        disabled={busy === placeholder.id || !picked[placeholder.id]}
                        onClick={() => reassign(placeholder.id)}
                        className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
                      >
                        {busy === placeholder.id ? "Reassigning..." : "Reassign"}
                      </button>
                    </>
                  )}
                </>
              ) : (
                <span className="ml-auto text-xs text-gain">
                  Moved {moved} session{moved === 1 ? "" : "s"} to a real driver.
                </span>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
