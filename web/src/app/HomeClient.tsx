"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { ENGINE_CATEGORIES, engineColor } from "@/lib/engine";
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
  engineCategory: string | null;
  visibility: string;
  driverProfileId: number | null;
  driverName: string;
};

/**
 * One definition of the column layout, used by the header and every row.
 *
 * Two copies of a nine-column template is how a table ends up with headers
 * that no longer sit over their own data.
 */
const COLUMNS =
  "grid grid-cols-[24px_1.7fr_0.6fr_0.6fr_1.3fr_1.9fr_0.8fr_0.5fr_1.2fr] items-center gap-2";

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

// Water reads blue, and a mixed track reads as the warning it is. Dry stays
// plain, because "nothing unusual" should not compete for attention with the
// two conditions that change how the lap times should be read. The two hues
// are the validated categorical slots used on the track map, so they are
// legible on this surface and distinguishable to a colour-blind reader.
const CONDITION_COLOR: Record<string, string> = {
  Wet: "#3987e5",
  Mixed: "#d95926",
  Dry: "#eef0f1",
};
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

  // Bulk track naming. The sync tool has no idea what track it is at -- it
  // reads a logger, not a calendar -- so a day's worth of synced sessions
  // arrives with no track name at all, and naming thirty of them one at a
  // time through the edit row is not a thing anyone will do twice.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkTrack, setBulkTrack] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);

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

  /**
   * Driver, then day, then sessions.
   *
   * A track day is the unit people actually think in -- six sessions at one
   * circuit on one afternoon -- and repeating the date on all six rows spent
   * a column saying the same thing six times. Days run newest-first unless
   * the date sort is flipped; rows inside a day keep whatever sort is
   * selected, which for the default (date) means chronological within the
   * day.
   */
  const grouped = useMemo(() => {
    const byDriver = new Map<number | null, SessionRow[]>();
    for (const row of filtered) {
      const list = byDriver.get(row.driverProfileId) ?? [];
      list.push(row);
      byDriver.set(row.driverProfileId, list);
    }

    // Days newest-first by default, following the date sort when that is
    // what is selected -- otherwise "sort by date ascending" would reorder
    // rows inside each day while the days themselves stayed put.
    const dayDirection = sortKey === "startDate" && !sortDesc ? 1 : -1;

    // Your own sessions first, then everyone else alphabetically.
    return [...byDriver.entries()]
      .sort(([a, ra], [b, rb]) => {
        if (a === myProfileId) return -1;
        if (b === myProfileId) return 1;
        return ra[0].driverName.localeCompare(rb[0].driverName);
      })
      .map(([profileId, driverRows]) => {
        const byDay = new Map<string, SessionRow[]>();
        for (const row of driverRows) {
          const key = row.startDate ?? "";
          const list = byDay.get(key) ?? [];
          list.push(row);
          byDay.set(key, list);
        }
        const days = [...byDay.entries()].sort(([a], [b]) => {
          const at = parseSessionDate(a)?.getTime();
          const bt = parseSessionDate(b)?.getTime();
          if (at === undefined) return 1;
          if (bt === undefined) return -1;
          return (at - bt) * dayDirection;
        });
        return {
          profileId,
          driverName: driverRows[0].driverName,
          sessions: driverRows.length,
          tracks: new Set(driverRows.map((r) => r.trackName)).size,
          days,
        };
      });
  }, [filtered, myProfileId, sortKey, sortDesc]);

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

  /**
   * Name every selected session's track in one statement.
   *
   * `.in(...)` rather than a request per row: thirty sequential PATCHes is
   * thirty chances to half-apply. RLS still decides which of them land --
   * `sessions_update_own` filters the set server-side, so this cannot rename
   * a teammate's session even if one were somehow selected.
   */
  async function applyBulkTrack() {
    const ids = [...selected];
    const name = bulkTrack.trim();
    if (ids.length === 0 || !name) return;

    setBulkBusy(true);
    setError(null);
    const { error: updateError } = await createClient()
      .from("sessions")
      .update({ track_name: name })
      .in("id", ids);
    setBulkBusy(false);
    if (updateError) {
      setError(updateError.message);
      return;
    }
    setRows((current) =>
      current.map((r) => (selected.has(r.id) ? { ...r, trackName: name } : r)),
    );
    setSelected(new Set());
    setBulkTrack("");
  }

  function toggleSelected(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  /** Select or clear a whole day at once -- the way sessions actually arrive. */
  function toggleDay(dayRows: SessionRow[]) {
    const ids = dayRows.filter((r) => r.driverProfileId === myProfileId).map((r) => r.id);
    const allSelected = ids.length > 0 && ids.every((id) => selected.has(id));
    setSelected((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (allSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
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

      {/* The date column became the day heading, so its sort control moves
          here rather than disappearing with it. */}
      <div className="mb-2 flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            if (sortKey === "startDate") setSortDesc(!sortDesc);
            else {
              setSortKey("startDate");
              setSortDesc(true);
            }
          }}
          className="label hover:text-ink"
        >
          Days: {sortKey === "startDate" && !sortDesc ? "oldest first ↑" : "newest first ↓"}
        </button>
      </div>

      {selected.size > 0 && (
        <div className="mb-3 flex flex-wrap items-end gap-3 rounded border border-accent/50 bg-raised px-3 py-2">
          <span className="text-sm font-semibold">
            {selected.size} session{selected.size === 1 ? "" : "s"} selected
          </span>
          <label className="block">
            <span className="label mb-1 block">Set track name</span>
            <input
              list="home-track-options"
              value={bulkTrack}
              onChange={(e) => setBulkTrack(e.target.value)}
              placeholder="Barmosen"
              className="rounded border border-hairline bg-surface px-2 py-1 text-sm"
            />
          </label>
          <datalist id="home-track-options">
            {trackOptions.map((option) => (
              <option key={option} value={option} />
            ))}
          </datalist>
          <button
            type="button"
            disabled={bulkBusy || !bulkTrack.trim()}
            onClick={applyBulkTrack}
            className="rounded bg-accent px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-40"
          >
            {bulkBusy ? "Applying..." : `Apply to ${selected.size}`}
          </button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="text-sm text-muted underline"
          >
            Clear selection
          </button>
        </div>
      )}

      <div className="overflow-x-auto">
        <div className="min-w-[880px]">
          <div className={`${COLUMNS} border-b border-hairline pb-1`}>
            <span className="label" />
            <SortHeader label="Track" column="trackName" />
            <span className="label">Time</span>
            <span className="label">Laps</span>
            <span className="label">Type</span>
            <span className="label">Class / conditions</span>
            <SortHeader label="Best lap" column="bestLapS" />
            <span className="label" title="Only you can see a private session">
              Private
            </span>
            <span className="label" />
          </div>

          {filtered.length === 0 ? (
            <p className="py-6 text-sm text-muted">No sessions match these filters.</p>
          ) : (
            grouped.map((driver) => {
              const isMine = driver.profileId === myProfileId;
              return (
                <section key={String(driver.profileId)}>
                  <div className="mt-3 flex items-baseline gap-3 border-b border-hairline pb-1">
                    <span className="text-sm font-bold">
                      {isMine ? "👤 " : "🏁 "}
                      {driver.driverName}
                      {isMine && " (you)"}
                    </span>
                    <span className="text-[11px] text-muted">
                      {driver.sessions} session{driver.sessions === 1 ? "" : "s"} ·{" "}
                      {driver.tracks} track(s)
                    </span>
                  </div>

                  {driver.days.map(([day, dayRows]) => {
                    const selectable = isMine ? dayRows.map((r) => r.id) : [];
                    const allSelected =
                      selectable.length > 0 && selectable.every((id) => selected.has(id));
                    return (
                      <div key={day || "undated"}>
                        <div className="mt-2 flex items-center gap-2 py-1 pl-1">
                          {isMine && (
                            <input
                              type="checkbox"
                              checked={allSelected}
                              onChange={() => toggleDay(dayRows)}
                              title="Select this day -- then set the track name for all of it at once"
                              className="h-3.5 w-3.5 accent-[#3987e5]"
                            />
                          )}
                          <span className="text-xs font-semibold text-ink2">
                            {day ? sessionDate(day) : "Date unknown"}
                          </span>
                          <span className="text-[11px] text-muted">
                            {dayRows.length} session{dayRows.length === 1 ? "" : "s"}
                          </span>
                        </div>

                        {dayRows.map((row) => {
                          const shown = displayType(row.sessionType);
                          const typeOptionsForRow = SESSION_TYPES.includes(shown.label)
                            ? SESSION_TYPES
                            : [...SESSION_TYPES, shown.label];
                          return (
                            <div key={row.id}>
                              <div className={`${COLUMNS} border-b border-hairline/60 py-1 hover:bg-rowalt`}>
                                <span className="pl-1">
                                  {isMine && (
                                    <input
                                      type="checkbox"
                                      checked={selected.has(row.id)}
                                      onChange={() => toggleSelected(row.id)}
                                      title="Select for bulk track naming"
                                      className="h-3.5 w-3.5 accent-[#3987e5]"
                                    />
                                  )}
                                </span>
                                <Link
                                  href={`/sessions/${row.id}`}
                                  className={`truncate text-xs font-bold hover:text-accent hover:underline ${
                                    row.trackName ? "" : "text-muted italic"
                                  }`}
                                  title="Open lap analysis"
                                >
                                  {row.trackName || "Unknown track"}
                                </Link>
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

                                <span className="flex items-center gap-1 overflow-hidden">
                                  {row.kartClass && (
                                    <span className="whitespace-nowrap rounded bg-selected px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink2">
                                      {row.kartClass}
                                    </span>
                                  )}
                                  {row.engineCategory && (
                                    // Coloured by family, and still spelled out --
                                    // the colour speeds up scanning, it does not
                                    // carry the meaning on its own.
                                    <span
                                      className="truncate text-[10px] font-semibold"
                                      style={{ color: engineColor(row.engineCategory) ?? undefined }}
                                      title={`Engine: ${row.engineCategory}`}
                                    >
                                      {row.engineCategory}
                                    </span>
                                  )}
                                  <select
                                    value={row.trackCondition ?? ""}
                                    disabled={!isMine || busy === row.id}
                                    onChange={(e) =>
                                      patch(
                                        row.id,
                                        { track_condition: e.target.value || null },
                                        { trackCondition: e.target.value || null },
                                      )
                                    }
                                    style={{
                                      color: row.trackCondition
                                        ? CONDITION_COLOR[row.trackCondition]
                                        : undefined,
                                    }}
                                    className="rounded border border-hairline bg-surface px-1 py-0.5 text-[11px] disabled:opacity-60"
                                  >
                                    <option value="" style={{ color: "#8c959c" }}>
                                      —
                                    </option>
                                    {CONDITIONS.map((option) => (
                                      <option key={option} value={option} style={{ color: "#eef0f1" }}>
                                        {option}
                                      </option>
                                    ))}
                                  </select>
                                </span>

                                <span className="text-right font-mono text-xs font-bold">
                                  {lapTime(row.bestLapS)}
                                </span>

                                {/* Checked means private. Unchecked covers both
                                    shared and team-only, so the tooltip says which
                                    -- and the edit row keeps the three-way choice,
                                    since a checkbox cannot express it. */}
                                <span className="flex justify-center">
                                  <input
                                    type="checkbox"
                                    checked={row.visibility === "private"}
                                    disabled={!isMine || busy === row.id}
                                    onChange={() => {
                                      const next = row.visibility === "private" ? "shared" : "private";
                                      patch(row.id, { visibility: next }, { visibility: next });
                                    }}
                                    title={
                                      isMine
                                        ? `Currently ${VISIBILITY_LABELS[row.visibility] ?? row.visibility}`
                                        : VISIBILITY_LABELS[row.visibility] ?? row.visibility
                                    }
                                    className="h-3.5 w-3.5 accent-[#3987e5] disabled:opacity-40"
                                  />
                                </span>

                                <span className="flex items-center justify-end gap-2 text-[11px]">
                                  {isMine && (
                                    <button
                                      type="button"
                                      onClick={() => setEditing(editing === row.id ? null : row.id)}
                                      className="text-muted underline hover:text-ink"
                                    >
                                      Edit
                                    </button>
                                  )}
                                  <Link
                                    href={`/sessions/${row.id}`}
                                    className="rounded border border-hairline bg-raised px-3 py-1 font-semibold text-ink2 hover:border-accent hover:text-ink"
                                  >
                                    Open
                                  </Link>
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
  const [engine, setEngine] = useState(row.engineCategory ?? "");
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
      {/* Stored per session, not read from your profile, so a class change
          mid-season does not relabel everything you have ever driven. That
          also means an old session can be wrong, which is what this fixes. */}
      <label className="block">
        <span className="label mb-1 block">Engine</span>
        <select
          value={engine}
          onChange={(e) => setEngine(e.target.value)}
          style={{ color: engineColor(engine) ?? undefined }}
          className="rounded border border-hairline bg-canvas px-2 py-1 text-sm"
        >
          <option value="" style={{ color: "#8c959c" }}>
            Not recorded
          </option>
          {ENGINE_CATEGORIES.map((option) => (
            <option key={option} value={option} style={{ color: "#eef0f1" }}>
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
              engine_category: engine || null,
              visibility,
            },
            {
              trackName: trackName.trim() || null,
              trackCondition: condition || null,
              engineCategory: engine || null,
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
