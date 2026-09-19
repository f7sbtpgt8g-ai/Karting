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

const ADD_NEW_DRIVER = "__new__";

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
 *
 * The picker also offers "+ Add a new driver..." for a teammate who
 * hasn't registered yet (`driver_profiles_insert_placeholder`, 0017) --
 * see PlaceholderDrivers for moving that placeholder's sessions onto a
 * real profile once they do.
 */
export default function UnassignedSessions({
  sessions,
  profiles,
  appUserId,
}: {
  sessions: UnassignedSessionRow[];
  profiles: Profile[];
  appUserId: number;
}) {
  const router = useRouter();
  const [rows, setRows] = useState(sessions);
  const [knownProfiles, setKnownProfiles] = useState(profiles);
  const [picked, setPicked] = useState<Record<number, string>>({});
  const [addingFor, setAddingFor] = useState<number | null>(null);
  const [newDriverName, setNewDriverName] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (rows.length === 0) return null;

  function pick(rowId: number, value: string) {
    if (value === ADD_NEW_DRIVER) {
      setAddingFor(rowId);
      setNewDriverName("");
      return;
    }
    setPicked((current) => ({ ...current, [rowId]: value }));
  }

  async function addDriver(rowId: number) {
    const name = newDriverName.trim();
    if (!name) return;
    setBusy(rowId);
    setError(null);
    const { data, error: insertError } = await createClient()
      .from("driver_profiles")
      .insert({
        display_name: name,
        claim_status: "unclaimed",
        created_by_user_id: appUserId,
      })
      .select("id, display_name")
      .single();
    setBusy(null);
    if (insertError || !data) {
      setError(insertError?.message || "Could not add that driver.");
      return;
    }
    setKnownProfiles((current) =>
      [...current, { id: data.id, display_name: data.display_name }].sort((a, b) =>
        a.display_name.localeCompare(b.display_name),
      ),
    );
    setPicked((current) => ({ ...current, [rowId]: String(data.id) }));
    setAddingFor(null);
  }

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
        Uploaded with &quot;Decide after parsing&quot; -- pick who drove each one below, or add a
        driver who hasn&apos;t registered yet. Until then these won&apos;t show up on Home or
        anywhere else, only here.
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

            {addingFor === row.id ? (
              <span className="ml-auto flex items-center gap-2">
                <input
                  autoFocus
                  value={newDriverName}
                  onChange={(e) => setNewDriverName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") addDriver(row.id);
                    if (e.key === "Escape") setAddingFor(null);
                  }}
                  placeholder="Driver's name"
                  disabled={busy === row.id}
                  className="rounded border border-hairline bg-canvas px-2 py-1 text-xs disabled:opacity-60"
                />
                <button
                  type="button"
                  disabled={busy === row.id || !newDriverName.trim()}
                  onClick={() => addDriver(row.id)}
                  className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
                >
                  {busy === row.id ? "Adding..." : "Add"}
                </button>
                <button
                  type="button"
                  onClick={() => setAddingFor(null)}
                  className="text-xs text-muted underline"
                >
                  Cancel
                </button>
              </span>
            ) : (
              <>
                <select
                  value={picked[row.id] ?? ""}
                  disabled={busy === row.id}
                  onChange={(e) => pick(row.id, e.target.value)}
                  className="ml-auto rounded border border-hairline bg-canvas px-2 py-1 text-xs disabled:opacity-60"
                >
                  <option value="">Choose a driver...</option>
                  {knownProfiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.display_name}
                    </option>
                  ))}
                  <option value={ADD_NEW_DRIVER}>+ Add a new driver...</option>
                </select>
                <button
                  type="button"
                  disabled={busy === row.id || !picked[row.id]}
                  onClick={() => assign(row.id)}
                  className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
                >
                  {busy === row.id ? "Assigning..." : "Assign"}
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
