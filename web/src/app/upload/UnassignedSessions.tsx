"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { lapTime, sessionDate, sessionTime } from "@/lib/format";

export type UnassignedSessionRow = {
  id: number;
  track_name: string | null;
  session_type: string | null;
  start_date: string | null;
  start_time: string | null;
  n_laps: number | null;
  best_lap_s: number | null;
};

type Profile = { id: number; display_name: string };

/**
 * Sessions from an upload where "Decide after parsing" was picked --
 * `driver_profile_id` is NULL, which is exactly why they need a home of
 * their own: Home's query (`driver_profile_id IN (...)`) never matches
 * NULL, so an unassigned session is otherwise saved but invisible
 * everywhere in the app. This is that home -- pick a driver, and it takes
 * the same path a directly-assigned upload always has (`sessions_update_own`,
 * 0002, permits the uploader to set `driver_profile_id` on their own
 * upload to any profile RLS lets them see).
 *
 * Deliberately session-level, not batch-level: one multi-driver logger
 * file's batch can leave some sessions assigned and others not, and
 * "Recent uploads" already covers the batch's own status.
 */
export default function UnassignedSessions({
  sessions,
  profiles,
}: {
  sessions: UnassignedSessionRow[];
  profiles: Profile[];
}) {
  const router = useRouter();
  const [rows, setRows] = useState(sessions);
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (rows.length === 0) return null;

  async function assign(sessionId: number) {
    const profileId = picked[sessionId];
    if (!profileId) return;
    setBusy(sessionId);
    setError(null);
    const { data, error: updateError } = await createClient()
      .from("sessions")
      .update({ driver_profile_id: Number(profileId) })
      .eq("id", sessionId)
      .select("id");
    setBusy(null);
    if (updateError) {
      setError(updateError.message);
      return;
    }
    if (!data || data.length === 0) {
      setError("That session could not be assigned -- it may already have been claimed elsewhere.");
      return;
    }
    setRows((current) => current.filter((r) => r.id !== sessionId));
    router.refresh();
  }

  return (
    <section className="mb-10 rounded border border-accent/50 bg-raised p-4">
      <h2 className="mb-1 text-sm font-bold">
        {rows.length} session{rows.length === 1 ? "" : "s"} need{rows.length === 1 ? "s" : ""} a driver
      </h2>
      <p className="mb-3 text-xs text-muted">
        Uploaded with &quot;Decide after parsing&quot; -- pick who drove each one below. Until then
        these won&apos;t show up on Home or anywhere else, only here.
      </p>

      {error && (
        <p className="mb-3 text-sm text-loss" role="alert">
          {error}
        </p>
      )}

      <div className="space-y-2">
        {rows.map((row) => (
          <div
            key={row.id}
            className="flex flex-wrap items-center gap-2 rounded border border-hairline bg-surface px-3 py-2 text-sm"
          >
            <span className={`font-semibold ${row.track_name ? "" : "text-muted italic"}`}>
              {row.track_name || "Unknown track"}
            </span>
            <span className="text-xs text-muted">
              {row.start_date ? sessionDate(row.start_date) : "Date unknown"}
              {row.start_time ? ` · ${sessionTime(row.start_time)}` : ""}
              {" · "}
              {row.n_laps ?? 0} laps
              {row.best_lap_s != null && ` · ${lapTime(row.best_lap_s)}`}
              {row.session_type ? ` · ${row.session_type}` : ""}
            </span>

            <select
              value={picked[row.id] ?? ""}
              disabled={busy === row.id}
              onChange={(e) => setPicked((current) => ({ ...current, [row.id]: e.target.value }))}
              className="ml-auto rounded border border-hairline bg-canvas px-2 py-1 text-xs disabled:opacity-60"
            >
              <option value="">Choose a driver...</option>
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.display_name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={busy === row.id || !picked[row.id]}
              onClick={() => assign(row.id)}
              className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
            >
              {busy === row.id ? "Assigning..." : "Assign"}
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
