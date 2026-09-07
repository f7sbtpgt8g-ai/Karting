"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { sessionDate, sessionTime } from "@/lib/format";
import { engineColor } from "@/lib/engine";

/**
 * Which drivers a search covers.
 *
 * Three disjoint scopes rather than three overlapping ones: "mine" is your
 * own sessions, "team" is your teammates', "community" is everyone else's.
 * A driver looking for their own session from last week has one obvious
 * button, and the community list is not padded with sessions the other two
 * already offer.
 */
export type SearchScope = "mine" | "team" | "community";

export const SCOPE_LABEL: Record<SearchScope, string> = {
  mine: "Add session",
  team: "Add teammate",
  community: "Add community",
};

type Candidate = {
  id: number;
  driverName: string;
  startDate: string | null;
  startTime: string | null;
  sessionType: string | null;
  trackCondition: string | null;
  engineCategory: string | null;
  bestLapS: number | null;
  nLaps: number | null;
};

type RawRow = {
  id: number;
  track_name: string | null;
  session_type: string | null;
  start_date: string | null;
  start_time: string | null;
  track_condition: string | null;
  engine_category: string | null;
  best_lap_s: number | null;
  n_laps: number | null;
  driver_profile_id: number | null;
  driver_profiles: { display_name: string } | null;
};

const COLUMNS =
  "id, track_name, session_type, start_date, start_time, track_condition, " +
  "engine_category, best_lap_s, n_laps, driver_profile_id, driver_profiles(display_name)";

function lapClock(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(3).padStart(6, "0")}`;
}

/**
 * Pick sessions to compare against.
 *
 * Every search is restricted to the track of the session being viewed.
 * Comparing lap times between two circuits is not a comparison, and the
 * charts plot against distance travelled, so laps of different lengths would
 * be drawn over each other as though the corners lined up.
 *
 * The candidate list is whatever RLS returns. There is no visibility filter
 * in this component on purpose: `sessions_select` is the rule, and writing a
 * second version of it here would be a second thing to keep correct.
 */
export default function AddSessionsDialog({
  scope,
  trackName,
  myProfileId,
  excludeSessionIds,
  onCancel,
  onAdd,
}: {
  scope: SearchScope;
  trackName: string | null;
  myProfileId: number | null;
  excludeSessionIds: number[];
  onCancel: () => void;
  onAdd: (sessionIds: number[]) => void;
}) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [query, setQuery] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);

  // Escape closes, and focus starts inside the dialog rather than wherever
  // the page happened to leave it.
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const search = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNotice(null);
    const supabase = createClient();

    try {
      if (!trackName) {
        setCandidates([]);
        setNotice(
          "This session has no track name, so there is nothing to match other sessions against. " +
            "Name the track on Home first.",
        );
        return;
      }

      let profileIds: number[] | null = null;

      if (scope === "mine") {
        if (myProfileId === null) {
          setCandidates([]);
          setNotice("Your account has no driver profile yet, so it has no other sessions.");
          return;
        }
        profileIds = [myProfileId];
      }

      if (scope === "team") {
        if (myProfileId === null) {
          setCandidates([]);
          setNotice("Your account has no driver profile yet, so it is not on a team.");
          return;
        }
        const { data: mine, error: teamError } = await supabase
          .from("team_memberships")
          .select("team_id")
          .eq("driver_profile_id", myProfileId)
          .eq("status", "active");
        if (teamError) throw teamError;

        const teamIds = (mine ?? []).map((row) => row.team_id as number);
        if (teamIds.length === 0) {
          setCandidates([]);
          setNotice(
            "You are not on a team yet. Once you join or create one, your teammates' sessions " +
              "at this track will show up here. Add community searches every driver instead.",
          );
          return;
        }

        const { data: roster, error: rosterError } = await supabase
          .from("team_memberships")
          .select("driver_profile_id")
          .in("team_id", teamIds)
          .eq("status", "active");
        if (rosterError) throw rosterError;

        profileIds = Array.from(
          new Set((roster ?? []).map((row) => row.driver_profile_id as number)),
        ).filter((id) => id !== myProfileId);

        if (profileIds.length === 0) {
          setCandidates([]);
          setNotice(
            "You are the only active member of your team, so there are no teammates' sessions " +
              "to compare against yet.",
          );
          return;
        }
      }

      let request = supabase
        .from("sessions")
        .select(COLUMNS)
        .eq("track_name", trackName)
        .order("start_date", { ascending: false })
        .limit(200);

      if (profileIds) request = request.in("driver_profile_id", profileIds);
      // Community is everyone else: your own sessions have their own button,
      // and listing them here would push a teammate's off the first screen.
      else if (myProfileId !== null) request = request.neq("driver_profile_id", myProfileId);

      const { data, error: searchError } = await request.returns<RawRow[]>();
      if (searchError) throw searchError;

      const excluded = new Set(excludeSessionIds);
      const rows = (data ?? [])
        .filter((row) => !excluded.has(row.id))
        .map((row) => ({
          id: row.id,
          driverName: row.driver_profiles?.display_name ?? "Unknown driver",
          startDate: row.start_date,
          startTime: row.start_time,
          sessionType: row.session_type,
          trackCondition: row.track_condition,
          engineCategory: row.engine_category,
          bestLapS: row.best_lap_s,
          nLaps: row.n_laps,
        }));

      setCandidates(rows);
      if (rows.length === 0) {
        setNotice(
          scope === "mine"
            ? `You have no other sessions at ${trackName}.`
            : `No sessions at ${trackName} from ${
                scope === "team" ? "your teammates" : "other drivers"
              } are shared with you.`,
        );
      }
    } catch (searchError) {
      setError(searchError instanceof Error ? searchError.message : String(searchError));
    } finally {
      setLoading(false);
    }
  }, [scope, trackName, myProfileId, excludeSessionIds]);

  useEffect(() => {
    void search();
  }, [search]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return candidates;
    return candidates.filter((row) =>
      [row.driverName, row.startDate, row.sessionType, row.trackCondition, row.engineCategory]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle)),
    );
  }, [candidates, query]);

  function toggle(id: number) {
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={SCOPE_LABEL[scope]}
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-lg border border-hairline bg-surface shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-hairline px-5 py-4">
          <div>
            <h2 className="text-sm font-bold">{SCOPE_LABEL[scope]}</h2>
            <p className="mt-0.5 text-xs text-muted">
              {scope === "mine"
                ? "Your own sessions"
                : scope === "team"
                  ? "Sessions from drivers on your teams"
                  : "Sessions shared by any driver"}
              {trackName ? ` at ${trackName}` : ""}. Only this track &mdash; laps from another
              circuit cannot be plotted against these.
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onCancel}
            aria-label="Close"
            className="rounded px-2 py-1 text-lg leading-none text-muted hover:text-ink"
          >
            &times;
          </button>
        </div>

        {candidates.length > 0 && (
          <div className="border-b border-hairline px-5 py-3">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by driver, date, type or engine"
              className="w-full rounded border border-hairline bg-canvas px-3 py-1.5 text-sm outline-none focus:border-accent"
            />
          </div>
        )}

        <div className="min-h-[8rem] flex-1 overflow-y-auto px-5 py-3">
          {loading ? (
            <p className="py-8 text-center text-sm text-muted">Searching...</p>
          ) : error ? (
            <p className="py-8 text-center text-sm text-loss" role="alert">
              {error}
            </p>
          ) : notice ? (
            <p className="mx-auto max-w-md py-8 text-center text-sm text-muted">{notice}</p>
          ) : filtered.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted">
              Nothing matches &ldquo;{query}&rdquo;.
            </p>
          ) : (
            <ul className="space-y-1">
              {filtered.map((row) => (
                <li key={row.id}>
                  <label
                    className={`flex cursor-pointer items-center gap-3 rounded border px-3 py-2 text-sm ${
                      picked.has(row.id)
                        ? "border-accent bg-selected"
                        : "border-transparent hover:bg-rowalt"
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={picked.has(row.id)}
                      onChange={() => toggle(row.id)}
                      className="h-3.5 w-3.5 accent-accent"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-semibold">{row.driverName}</span>
                      <span className="block truncate text-[11px] text-muted">
                        {sessionDate(row.startDate)} {sessionTime(row.startTime)}
                        {row.sessionType ? ` · ${row.sessionType}` : ""}
                        {row.trackCondition ? ` · ${row.trackCondition}` : ""}
                        {row.nLaps ? ` · ${row.nLaps} laps` : ""}
                      </span>
                    </span>
                    {row.engineCategory && (
                      <span
                        className="hidden shrink-0 text-[10px] font-semibold sm:block"
                        style={{ color: engineColor(row.engineCategory) ?? undefined }}
                      >
                        {row.engineCategory}
                      </span>
                    )}
                    <span className="shrink-0 font-mono text-xs font-bold">
                      {lapClock(row.bestLapS)}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-hairline px-5 py-3">
          <span className="text-xs text-muted">
            {picked.size > 0
              ? `${picked.size} session${picked.size === 1 ? "" : "s"} selected`
              : "Select one or more sessions"}
          </span>
          <span className="flex items-center gap-3">
            <button type="button" onClick={onCancel} className="text-sm text-muted underline">
              Cancel
            </button>
            <button
              type="button"
              disabled={picked.size === 0}
              onClick={() => onAdd([...picked])}
              className="rounded bg-accent px-5 py-1.5 text-sm font-semibold text-white disabled:opacity-40"
            >
              OK
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
