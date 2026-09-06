"use client";

import { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { lapTime, parseSessionDate, sessionDate, sessionTime } from "@/lib/format";

export type SessionRow = {
  id: number;
  trackName: string | null;
  sessionType: string | null;
  startDate: string | null;
  startTime: string | null;
  nLaps: number | null;
  bestLapS: number | null;
  trackCondition: string | null;
  kartClass: string | null;
  visibility: string;
  driverProfileId: number | null;
  driverName: string;
};

// app.py's HOME_SESSION_TYPE_OPTIONS and telemetry/weather.py's
// CONDITION_OPTIONS -- kept identical so a session typed in one app reads
// back correctly in the other.
const SESSION_TYPES = [
  "Training",
  "Warm up",
  "Free practice",
  "Qualifying",
  "Heat",
  "Superheat",
  "Final",
];
const CONDITIONS = ["Dry", "Wet", "Mixed"];
const VISIBILITY_LABELS: Record<string, string> = {
  private: "Private",
  team: "Team",
  shared: "Shared",
};

type SortKey = "trackName" | "startDate" | "bestLapS";

/**
 * A session whose `session_type` has never been saved reads back as an
 * unconfirmed "Training" -- the state is derived from the column being
 * empty rather than tracked separately, exactly as `_home_display_type`
 * does it. Unconfirmed shows in accent, confirmed in green.
 */
function displayType(raw: string | null): { label: string; confirmed: boolean } {
  if (raw && raw.trim()) return { label: raw, confirmed: true };
  return { label: SESSION_TYPES[0], confirmed: false };
}

export default function HomeClient({
  sessions,
  myProfileId,
  elevatedRole,
  onATeam,
}: {
  sessions: SessionRow[];
  myProfileId: number | null;
  elevatedRole: string | null;
  onATeam: boolean;
}) {
  const router = useRouter();
  const [rows, setRows] = useState(sessions);
  const [track, setTrack] = useState("");
  const [type, setType] = useState("");
  const [condition, setCondition] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("startDate");
  const [sortDesc, setSortDesc] = useState(true);
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const visibilityOptions = onATeam ? ["private", "team", "shared"] : ["private", "shared"];

  const trackOptions = useMemo(
    () => Array.from(new Set(rows.map((r) => r.trackName).filter(Boolean) as string[])).sort(),
    [rows],
  );
  const typeOptions = useMemo(
    () => Array.from(new Set(rows.map((r) => displayType(r.sessionType).label))).sort(),
    [rows],
  );
  const conditionOptions = useMemo(
    () => CONDITIONS.filter((c) => rows.some((r) => r.trackCondition === c)),
    [rows],
  );

  const stats = useMemo(
    () => [
      ["Drivers", new Set(rows.map((r) => r.driverProfileId)).size],
      ["Sessions", rows.length],
      ["Total laps", rows.reduce((sum, r) => sum + (r.nLaps ?? 0), 0)],
      ["Tracks", new Set(rows.map((r) => r.trackName).filter(Boolean)).size],
    ],
    [rows],
  );

  const filtered = useMemo(() => {
    const fromDate = from ? new Date(from) : null;
    const toDate = to ? new Date(to) : null;
    const matched = rows.filter((row) => {
      if (track && row.trackName !== track) return false;
      if (type && displayType(row.sessionType).label !== type) return false;
      if (condition && row.trackCondition !== condition) return false;
      if (fromDate || toDate) {
        const date = parseSessionDate(row.startDate);
        if (!date) return false;
        if (fromDate && date < fromDate) return false;
        if (toDate && date > toDate) return false;
      }
      return true;
    });

    const direction = sortDesc ? -1 : 1;
    return [...matched].sort((a, b) => {
      if (sortKey === "startDate") {
        // Sorted on a parsed date, not the raw DD-MM-YYYY text, which
        // would order 02-01-2026 before 15-12-2025.
        const at = parseSessionDate(a.startDate)?.getTime();
        const bt = parseSessionDate(b.startDate)?.getTime();
        if (at === undefined) return 1;
        if (bt === undefined) return -1;
        return (at - bt) * direction;
      }
      if (sortKey === "bestLapS") {
        // A session with no best lap sorts last either way -- it is
        // missing data, not an infinitely slow lap.
        if (a.bestLapS == null) return 1;
        if (b.bestLapS == null) return -1;
        return (a.bestLapS - b.bestLapS) * direction;
      }
      return (a.trackName ?? "").localeCompare(b.trackName ?? "") * direction;
    });
  }, [rows, track, type, condition, from, to, sortKey, sortDesc]);

  const grouped = useMemo(() => {
    const byDriver = new Map<number | null, SessionRow[]>();
    for (const row of filtered) {
      const list = byDriver.get(row.driverProfileId) ?? [];
      list.push(row);
      byDriver.set(row.driverProfileId, list);
    }
    // Your own sessions first, then everyone else alphabetically.
    return [...byDriver.entries()].sort(([a, ra], [b, rb]) => {
      if (a === myProfileId) return -1;
      if (b === myProfileId) return 1;
      return ra[0].driverName.localeCompare(rb[0].driverName);
    });
  }, [filtered, myProfileId]);

  async function patch(id: number, changes: Partial<Record<string, unknown>>, local: Partial<SessionRow>) {
    setBusy(id);
    setError(null);
    const { error: updateError } = await createClient()
      .from("sessions")
      .update(changes)
      .eq("id", id);
    setBusy(null);
    if (updateError) {
      setError(updateError.message);
      return false;
    }
    setRows((current) => current.map((r) => (r.id === id ? { ...r, ...local } : r)));
    return true;
  }

  async function remove(id: number) {
    setBusy(id);
    setError(null);
    const { error: deleteError } = await createClient().from("sessions").delete().eq("id", id);
    setBusy(null);
    if (deleteError) {
      setError(deleteError.message);
      return;
    }
    setRows((current) => current.filter((r) => r.id !== id));
    router.refresh();
  }

  function SortHeader({ label, column }: { label: string; column: SortKey }) {
    const active = sortKey === column;
    return (
      <button
        type="button"
        onClick={() => {
          if (active) setSortDesc(!sortDesc);
          else {
            setSortKey(column);
            setSortDesc(column === "startDate");
          }
        }}
        className={`label text-left ${active ? "text-ink" : ""}`}
      >
        {label}
        {active && (sortDesc ? " ↓" : " ↑")}
      </button>
    );
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap gap-2">
        {stats.map(([label, value]) => (
          <div key={label} className="min-w-[110px] rounded-md border border-hairline bg-raised px-4 py-2">
            <div className="label">{label}</div>
            <div className="font-mono text-2xl font-bold">{value}</div>
          </div>
        ))}
      </div>

      {elevatedRole && (
        <p className="mb-4 text-xs text-muted">
          Showing your sessions plus every teammate&apos;s team-or-shared session &mdash;
          you&apos;re this team&apos;s {elevatedRole}.
        </p>
      )}

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="block">
          <span className="label mb-1 block">Track</span>
          <select
            value={track}
            onChange={(e) => setTrack(e.target.value)}
            className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
          >
            <option value="">All tracks</option>
            {trackOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="label mb-1 block">Session type</span>
          <select
            value={type}
            onChange={(e) => setType(e.target.value)}
            className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
          >
            <option value="">All types</option>
            {typeOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="label mb-1 block">Conditions</span>
          <select
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
            className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
          >
            <option value="">All conditions</option>
            {conditionOptions.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <div className="flex gap-2">
          <label className="block flex-1">
            <span className="label mb-1 block">From</span>
            <input
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
            />
          </label>
          <label className="block flex-1">
            <span className="label mb-1 block">To</span>
            <input
              type="date"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
            />
          </label>
        </div>
      </div>

      {error && (
        <p className="mb-3 text-sm text-loss" role="alert">
          {error}
        </p>
      )}

      <div className="overflow-x-auto">
        <div className="min-w-[820px]">
          <div className="grid grid-cols-[2fr_1fr_0.7fr_0.7fr_1.4fr_1.4fr_1fr_1.6fr] gap-2 border-b border-hairline pb-1">
            <SortHeader label="Track" column="trackName" />
            <SortHeader label="Date" column="startDate" />
            <span className="label">Time</span>
            <span className="label">Laps</span>
            <span className="label">Type</span>
            <span className="label">Class / conditions</span>
            <SortHeader label="Best lap" column="bestLapS" />
            <span className="label" />
          </div>

          {filtered.length === 0 ? (
            <p className="py-6 text-sm text-muted">No sessions match these filters.</p>
          ) : (
            grouped.map(([profileId, driverRows]) => {
              const isMine = profileId === myProfileId;
              return (
                <section key={String(profileId)}>
                  <div className="mt-3 flex items-baseline gap-3 border-b border-hairline pb-1">
                    <span className="text-sm font-bold">
                      {isMine ? "👤 " : "🏁 "}
                      {driverRows[0].driverName}
                      {isMine && " (you)"}
                    </span>
                    <span className="text-[11px] text-muted">
                      {driverRows.length} session{driverRows.length === 1 ? "" : "s"} ·{" "}
                      {new Set(driverRows.map((r) => r.trackName)).size} track(s)
                    </span>
                  </div>

                  {driverRows.map((row) => {
                    const shown = displayType(row.sessionType);
                    const typeOptionsForRow = SESSION_TYPES.includes(shown.label)
                      ? SESSION_TYPES
                      : [...SESSION_TYPES, shown.label];
                    return (
                      <div key={row.id}>
                        <div className="grid grid-cols-[2fr_1fr_0.7fr_0.7fr_1.4fr_1.4fr_1fr_1.6fr] items-center gap-2 border-b border-hairline/60 py-1 hover:bg-rowalt">
                          <span className="truncate text-xs font-bold">
                            {row.trackName || "Unknown track"}
                          </span>
                          <span className="text-[11px] text-muted">{sessionDate(row.startDate)}</span>
                          <span className="text-[11px] text-muted">{sessionTime(row.startTime)}</span>
                          <span className="text-[11px] text-muted">{row.nLaps ?? 0} laps</span>

                          <select
                            value={shown.label}
                            disabled={!isMine || busy === row.id}
                            onChange={(e) =>
                              patch(row.id, { session_type: e.target.value }, { sessionType: e.target.value })
                            }
                            style={{ color: shown.confirmed ? "#2fd07a" : "#ff3b1f" }}
                            className="rounded border border-hairline bg-surface px-1 py-0.5 text-[11px] disabled:opacity-60"
                          >
                            {typeOptionsForRow.map((option) => (
                              <option key={option} value={option} style={{ color: "#eef0f1" }}>
                                {option}
                              </option>
                            ))}
                          </select>

                          <span className="flex gap-1 overflow-hidden">
                            {[row.kartClass, row.trackCondition].filter(Boolean).map((badge) => (
                              <span
                                key={badge}
                                className="whitespace-nowrap rounded bg-selected px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink2"
                              >
                                {badge}
                              </span>
                            ))}
                            {!row.kartClass && !row.trackCondition && (
                              <span className="text-[11px] text-muted">—</span>
                            )}
                          </span>

                          <span className="text-right font-mono text-xs font-bold">
                            {lapTime(row.bestLapS)}
                          </span>

                          <span className="flex justify-end gap-3 text-[11px]">
                            {isMine ? (
                              <>
                                <button
                                  type="button"
                                  onClick={() => setEditing(editing === row.id ? null : row.id)}
                                  className="text-muted underline hover:text-ink"
                                >
                                  Edit
                                </button>
                                <span className="rounded bg-selected px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-ink2">
                                  {VISIBILITY_LABELS[row.visibility] ?? row.visibility}
                                </span>
                              </>
                            ) : (
                              <span className="text-muted">—</span>
                            )}
                          </span>
                        </div>

                        {editing === row.id && isMine && (
                          <EditRow
                            row={row}
                            visibilityOptions={visibilityOptions}
                            busy={busy === row.id}
                            onCancel={() => setEditing(null)}
                            onSave={async (changes, local) => {
                              if (await patch(row.id, changes, local)) setEditing(null);
                            }}
                            onDelete={async () => {
                              await remove(row.id);
                              setEditing(null);
                            }}
                          />
                        )}
                      </div>
                    );
                  })}
                </section>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

function EditRow({
  row,
  visibilityOptions,
  busy,
  onCancel,
  onSave,
  onDelete,
}: {
  row: SessionRow;
  visibilityOptions: string[];
  busy: boolean;
  onCancel: () => void;
  onSave: (
    changes: Record<string, unknown>,
    local: Partial<SessionRow>,
  ) => void | Promise<void>;
  onDelete: () => void | Promise<void>;
}) {
  const [trackName, setTrackName] = useState(row.trackName ?? "");
  const [condition, setCondition] = useState(row.trackCondition ?? "");
  const [visibility, setVisibility] = useState(row.visibility);
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <div className="flex flex-wrap items-end gap-3 border-b border-hairline bg-surface px-3 py-3">
      <label className="block">
        <span className="label mb-1 block">Track name</span>
        <input
          value={trackName}
          onChange={(e) => setTrackName(e.target.value)}
          className="rounded border border-hairline bg-canvas px-2 py-1 text-sm"
        />
      </label>
      <label className="block">
        <span className="label mb-1 block">Conditions</span>
        <select
          value={condition}
          onChange={(e) => setCondition(e.target.value)}
          className="rounded border border-hairline bg-canvas px-2 py-1 text-sm"
        >
          <option value="">Unknown</option>
          {CONDITIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        <span className="label mb-1 block">Visibility</span>
        <select
          value={visibility}
          onChange={(e) => setVisibility(e.target.value)}
          className="rounded border border-hairline bg-canvas px-2 py-1 text-sm"
        >
          {visibilityOptions.map((option) => (
            <option key={option} value={option}>
              {VISIBILITY_LABELS[option]}
            </option>
          ))}
        </select>
      </label>

      <button
        type="button"
        disabled={busy}
        onClick={() =>
          onSave(
            {
              track_name: trackName.trim() || null,
              track_condition: condition || null,
              visibility,
            },
            {
              trackName: trackName.trim() || null,
              trackCondition: condition || null,
              visibility,
            },
          )
        }
        className="rounded bg-accent px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
      >
        {busy ? "Saving..." : "Save"}
      </button>
      <button type="button" onClick={onCancel} className="text-sm text-muted underline">
        Cancel
      </button>

      <span className="ml-auto flex items-center gap-2">
        {confirmDelete ? (
          <>
            <span className="text-xs text-loss">Delete permanently?</span>
            <button
              type="button"
              disabled={busy}
              onClick={onDelete}
              className="rounded bg-loss px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
            >
              Confirm
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(false)}
              className="text-sm text-muted underline"
            >
              Keep
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            className="text-sm text-loss underline"
          >
            Delete session
          </button>
        )}
      </span>
    </div>
  );
}
