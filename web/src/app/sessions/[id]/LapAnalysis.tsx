"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { createClient } from "@/lib/supabase/client";
import { sessionDate, sessionTime } from "@/lib/format";
import TrackMap from "./TrackMap";
import { sectorColor } from "@/lib/trackMap";
import LapCharts from "./LapCharts";
import AddSessionsDialog, { SCOPE_LABEL, type SearchScope } from "./AddSessionsDialog";
import type { ComparedLap, LapTrace } from "@/lib/lapCharts";
import { lapKey, lapLabel, lapsBySession, tabLabels } from "@/lib/comparison";
import { loadSessionBundle, type LapRow, type SessionBundle } from "@/lib/sessionBundle";
import { DriverName } from "@/components/CountryFlag";
import {
  DEFAULT_SECTORS,
  MAX_SECTORS,
  MIN_SECTORS,
  buildSectors,
  sectorTimes,
  theoreticalBest,
} from "@/lib/sectors";

export type { LapRow };

/** `m:ss.mmm` -- a lap time reads as a time, not a bare number of seconds. */
function lapClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  return `${minutes}:${rest.toFixed(3).padStart(6, "0")}`;
}

/** `0:04.598` for sectors too, so columns line up against lap times. */
function splitClock(seconds: number | null): string {
  return seconds === null ? "—" : lapClock(seconds);
}

function deltaText(delta: number | null): string {
  if (delta === null) return "";
  if (Math.abs(delta) < 0.0005) return "ref";
  return `${delta > 0 ? "+" : "−"}${Math.abs(delta).toFixed(3)}`;
}

export default function LapAnalysis({
  initial,
  appUserId,
  myProfileId,
}: {
  initial: SessionBundle;
  appUserId: number | null;
  myProfileId: number | null;
}) {
  // Every open session, the one this page is for first. Added sessions are
  // loaded in the browser and appended; nothing here is written back, so
  // leaving the page is all it takes to get back to one session.
  const [bundles, setBundles] = useState<SessionBundle[]>([initial]);
  const [activeId, setActiveId] = useState(initial.sessionId);
  const [sectorCount, setSectorCount] = useState(DEFAULT_SECTORS);
  // Keyed `sessionId:lapNumber`, because a lap number alone stops being
  // unique the moment a second driver's session is open.
  const [compared, setCompared] = useState<Set<string>>(new Set());
  const [showExcluded, setShowExcluded] = useState(false);
  // Raw traces, keyed by `sessionId:lapNumber`. Labelling happens further
  // down in a memo rather than here, so renaming a tab -- or ticking a lap
  // in one -- does not re-fetch several hundred KB of telemetry.
  const [rawTraces, setRawTraces] = useState<Map<string, LapTrace>>(new Map());
  const [referenceKey, setReferenceKey] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<SearchScope | null>(null);
  const [adding, setAdding] = useState(false);

  const active = bundles.find((b) => b.sessionId === activeId) ?? bundles[0];
  const multiSession = bundles.length > 1;

  const labels = useMemo(() => tabLabels(bundles), [bundles]);
  const nameFor = useCallback(
    (sessionId: number) => {
      const label = labels.get(sessionId);
      const bundle = bundles.find((b) => b.sessionId === sessionId);
      return { name: label?.name ?? bundle?.driverName ?? "Session", qualifier: label?.qualifier ?? null };
    },
    [labels, bundles],
  );

  const sectors = useMemo(
    () => buildSectors(active.segments, sectorCount),
    [active.segments, sectorCount],
  );

  // A lap counts unless the detector flagged it or the driver excluded it.
  // Both are shown, and both are reversible -- see the excluded-laps panel.
  const isExcluded = (row: LapRow) => row.isOutlier || row.excludedByUser;

  const withSectors = useMemo(
    () =>
      active.laps.map((row) => ({
        row,
        sectors: sectorTimes(sectors, active.segments, new Map(Object.entries(row.segmentTimes))),
      })),
    [active.laps, sectors, active.segments],
  );

  const included = withSectors.filter(({ row }) => !isExcluded(row));
  const excluded = withSectors.filter(({ row }) => isExcluded(row));

  const stats = useMemo(() => {
    const times = included
      .map(({ row }) => row.lapTimeS)
      .filter((t): t is number => t !== null && !Number.isNaN(t));

    const bestLapTime = times.length ? Math.min(...times) : null;
    const bestLapNumber =
      bestLapTime === null
        ? null
        : (included.find(({ row }) => row.lapTimeS === bestLapTime)?.row.lapNumber ?? null);

    const mean = times.length ? times.reduce((a, b) => a + b, 0) / times.length : null;
    // Population standard deviation, matching `summarize_laps`.
    const stdDev =
      mean === null || times.length < 2
        ? null
        : Math.sqrt(times.reduce((sum, t) => sum + (t - mean) ** 2, 0) / times.length);

    const speeds = included.map(({ row }) => row.maxSpeedKmh).filter((v): v is number => v !== null);
    const revs = included.map(({ row }) => row.maxRpm).filter((v): v is number => v !== null);

    const theoretical = theoreticalBest(included.map(({ sectors: s }) => s));

    return {
      bestLapTime,
      bestLapNumber,
      stdDev,
      maxSpeed: speeds.length ? Math.max(...speeds) : null,
      maxRpm: revs.length ? Math.max(...revs) : null,
      theoretical,
      gain:
        theoretical.total !== null && bestLapTime !== null ? theoretical.total - bestLapTime : null,
    };
  }, [included]);

  // Fastest time in each sector column, across included laps only: an
  // excluded lap's sector should not win a purple highlight it cannot
  // contribute to the theoretical best.
  const fastestPerSector = stats.theoretical.perSector;

  async function toggleExcluded(row: LapRow) {
    const next = !row.excludedByUser;
    setBusy(row.lapNumber);
    setError(null);
    const { data, error: updateError } = await createClient()
      .from("laps")
      .update({ excluded_by_user: next })
      .eq("session_db_id", active.sessionId)
      .eq("lap_number", row.lapNumber)
      .select("lap_number");
    setBusy(null);
    if (updateError) {
      setError(updateError.message);
      return;
    }
    // An UPDATE that RLS filtered out is a success with nothing changed, not
    // an error -- so the row coming back is the only proof it was saved.
    if (!data || data.length === 0) {
      setError("That lap was not changed: it is not yours to edit.");
      return;
    }
    setBundles((current) =>
      current.map((bundle) =>
        bundle.sessionId === active.sessionId
          ? {
              ...bundle,
              laps: bundle.laps.map((r) =>
                r.lapNumber === row.lapNumber ? { ...r, excludedByUser: next } : r,
              ),
            }
          : bundle,
      ),
    );
  }

  async function addSessions(ids: number[]) {
    setDialog(null);
    setAdding(true);
    setError(null);
    const supabase = createClient();
    const loaded: SessionBundle[] = [];
    const skipped: number[] = [];
    for (const id of ids) {
      const bundle = await loadSessionBundle(supabase, id, appUserId);
      if (bundle) loaded.push(bundle);
      else skipped.push(id);
    }
    setAdding(false);
    if (skipped.length > 0) {
      setError(
        `${skipped.length} session${skipped.length === 1 ? "" : "s"} could not be opened: ` +
          "no stored analysis yet, so there are no lap traces to compare.",
      );
    }
    if (loaded.length === 0) return;
    setBundles((current) => [
      ...current,
      ...loaded.filter((b) => !current.some((existing) => existing.sessionId === b.sessionId)),
    ]);
    setActiveId(loaded[0].sessionId);
  }

  function closeSession(sessionId: number) {
    setBundles((current) => current.filter((b) => b.sessionId !== sessionId));
    setCompared((current) => new Set([...current].filter((key) => !key.startsWith(`${sessionId}:`))));
    if (activeId === sessionId) setActiveId(initial.sessionId);
  }

  // Trace arrays are fetched only for the laps actually selected, and per
  // session they belong to. Loading every lap's six arrays up front would be
  // ~400 KB per session to render a table that needs none of it.
  const selectedKey = [...compared].sort().join("|");
  useEffect(() => {
    const keys = selectedKey ? selectedKey.split("|") : [];
    if (keys.length === 0) {
      setRawTraces(new Map());
      return;
    }
    let cancelled = false;
    (async () => {
      const supabase = createClient();
      const grouped = lapsBySession(keys);
      const results = await Promise.all(
        [...grouped.entries()].map(async ([sessionId, lapNumbers]) => {
          const { data, error: traceError } = await supabase
            .from("lap_traces")
            .select("lap_number, distance_m, lap_time_s, speed_kmh, rpm, lateral_g, longitudinal_g")
            .eq("session_db_id", sessionId)
            .in("lap_number", lapNumbers)
            .order("lap_number");
          if (traceError) throw traceError;
          return { sessionId, rows: data ?? [] };
        }),
      ).catch((traceError: unknown) => {
        if (!cancelled) {
          setError(traceError instanceof Error ? traceError.message : String(traceError));
        }
        return null;
      });
      if (cancelled || results === null) return;

      const byKey = new Map<string, LapTrace>();
      for (const { sessionId, rows } of results) {
        for (const row of rows) {
          byKey.set(lapKey(sessionId, row.lap_number as number), {
            lapNumber: row.lap_number as number,
            distanceM: (row.distance_m as number[]) ?? [],
            lapTimeS: (row.lap_time_s as number[]) ?? [],
            speedKmh: (row.speed_kmh as (number | null)[]) ?? [],
            rpm: (row.rpm as (number | null)[]) ?? [],
            lateralG: (row.lateral_g as (number | null)[]) ?? [],
            longitudinalG: (row.longitudinal_g as (number | null)[]) ?? [],
          });
        }
      }
      setRawTraces(byKey);
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedKey]);

  // Ordered by the selection, not by the query, so a lap keeps its colour as
  // others are added and removed around it.
  const traces = useMemo(() => {
    const keys = selectedKey ? selectedKey.split("|") : [];
    return keys
      .map((key) => {
        const trace = rawTraces.get(key);
        if (!trace) return null;
        const sessionId = Number(key.split(":")[0]);
        const { name, qualifier } = nameFor(sessionId);
        return {
          key,
          sessionId,
          lapNumber: trace.lapNumber,
          label: lapLabel(trace.lapNumber, name, { multiSession, qualifier }),
          trace,
        } satisfies ComparedLap;
      })
      .filter((lap): lap is ComparedLap => lap !== null);
  }, [selectedKey, rawTraces, multiSession, nameFor]);

  // Default the delta reference to the quickest selected lap -- comparing
  // against the best of what is on screen is the question being asked most
  // of the time.
  useEffect(() => {
    if (traces.length === 0) {
      setReferenceKey(null);
      return;
    }
    setReferenceKey((current) => {
      if (current !== null && traces.some((t) => t.key === current)) return current;
      const quickest = [...traces]
        .map((t) => {
          const bundle = bundles.find((b) => b.sessionId === t.sessionId);
          const row = bundle?.laps.find((r) => r.lapNumber === t.lapNumber);
          return { key: t.key, time: row?.lapTimeS ?? null };
        })
        .filter((entry): entry is { key: string; time: number } => entry.time !== null)
        .sort((a, b) => a.time - b.time)[0];
      return quickest?.key ?? traces[0].key;
    });
  }, [traces, bundles]);

  function toggleCompared(sessionId: number, lapNumber: number) {
    const key = lapKey(sessionId, lapNumber);
    setCompared((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (active.dataError) {
    return (
      <div>
        <Header bundle={active} />
        <p className="rounded border border-hairline bg-surface p-6 text-sm text-muted">
          {active.dataError}
        </p>
      </div>
    );
  }

  const columns = `2.6rem 2.6rem 3rem 6rem repeat(${sectors.length}, minmax(5.5rem, 1fr)) 6rem 5.5rem`;

  return (
    <div>
      <Header bundle={active} />

      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Card label="Best lap" tone="ink">
          <span className="font-mono text-2xl font-bold">{lapClock(stats.bestLapTime)}</span>
          <span className="mt-1 block text-[11px] text-muted">
            {stats.bestLapNumber ? `Lap ${stats.bestLapNumber}` : "No clean laps"}
          </span>
        </Card>

        <Card label="Speed · RPM" tone="loss">
          <span className="font-mono text-2xl font-bold text-loss">
            {stats.maxSpeed !== null ? Math.round(stats.maxSpeed) : "—"}
            <span className="ml-1 text-[11px] font-normal text-muted">km/h</span>
          </span>
          <span className="mt-1 block font-mono text-sm text-loss">
            {stats.maxRpm !== null ? Math.round(stats.maxRpm).toLocaleString() : "—"}
            <span className="ml-1 text-[11px] font-normal text-muted">rpm</span>
          </span>
        </Card>

        <Card label="Total laps" tone="theoretical">
          <span className="font-mono text-2xl font-bold text-theoretical">
            {included.length}/{active.laps.length}
          </span>
          <span className="mt-1 block text-[11px] text-muted">{excluded.length} excl.</span>
        </Card>

        <Card label="Consistency" tone="gain">
          <span className="font-mono text-2xl font-bold text-gain">
            {stats.stdDev !== null ? `±${stats.stdDev.toFixed(2)}s` : "—"}
          </span>
          <span className="mt-1 block text-[11px] text-muted">
            over {included.length} lap{included.length === 1 ? "" : "s"}
          </span>
        </Card>

        <Card label="Theoretical best" tone="reference">
          <span className="font-mono text-2xl font-bold text-reference">
            {lapClock(stats.theoretical.total)}
          </span>
          <span className="mt-1 block font-mono text-[11px] text-muted">
            {stats.gain !== null
              ? `${stats.gain.toFixed(3)}s vs best`
              : `best ${sectors.length} sectors`}
          </span>
        </Card>
      </div>

      {active.speedIsEstimated && (
        <p className="mb-4 rounded border border-hairline bg-surface px-3 py-2 text-xs text-muted">
          This export has no GPS Speed channel. Speed is derived from GPS distance, so speed-based
          figures are estimates.
        </p>
      )}

      {active.peaksMissing && (
        <p className="mb-4 rounded border border-hairline bg-surface px-3 py-2 text-xs text-muted">
          Peak speed and RPM were added after this session was analysed, so they are blank.
          Re-running the analysis backfill fills them in.
        </p>
      )}

      <div className="mb-3 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-sm font-bold">Lap times</h2>
          <p className="text-xs text-muted">
            Purple is the fastest of the session shown. Delta is against its fastest lap.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {(["team", "community", "mine"] as SearchScope[]).map((scope) => (
            <button
              key={scope}
              type="button"
              disabled={adding}
              onClick={() => setDialog(scope)}
              className="rounded border border-hairline bg-raised px-3 py-1.5 text-xs font-semibold text-ink2 hover:border-accent hover:text-ink disabled:opacity-40"
            >
              {SCOPE_LABEL[scope]}
            </button>
          ))}
          <label className="ml-2 flex items-center gap-2">
            <span className="label">Sectors</span>
            <select
              value={sectorCount}
              onChange={(e) => setSectorCount(Number(e.target.value))}
              className="rounded border border-hairline bg-surface px-2 py-1 text-sm"
            >
              {Array.from({ length: MAX_SECTORS - MIN_SECTORS + 1 }, (_, i) => MIN_SECTORS + i).map(
                (n) => (
                  <option key={n} value={n}>
                    {n} sectors
                  </option>
                ),
              )}
            </select>
          </label>
        </div>
      </div>

      {/* Session tabs. The table, the cards and the track map all follow the
          selected one; the comparison below does not -- laps stay selected
          across tabs, which is the entire point of opening a second one. */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {bundles.map((bundle) => {
          const isActive = bundle.sessionId === activeId;
          const isInitial = bundle.sessionId === initial.sessionId;
          const { name, qualifier } = nameFor(bundle.sessionId);
          const selectedHere = [...compared].filter((key) =>
            key.startsWith(`${bundle.sessionId}:`),
          ).length;
          return (
            <span
              key={bundle.sessionId}
              className={`flex items-center gap-1.5 rounded-t border-b-2 px-3 py-1.5 text-xs ${
                isActive
                  ? "border-accent bg-selected font-semibold text-ink"
                  : "border-transparent bg-raised text-muted hover:text-ink"
              }`}
            >
              <button type="button" onClick={() => setActiveId(bundle.sessionId)}>
                {isInitial && bundle.canEdit ? "My laps" : name}
                {qualifier && <span className="ml-1 font-normal text-muted">{qualifier}</span>}
                {selectedHere > 0 && (
                  <span className="ml-1.5 rounded bg-reference/25 px-1 text-[10px] text-reference">
                    {selectedHere}
                  </span>
                )}
              </button>
              {!isInitial && (
                <button
                  type="button"
                  onClick={() => closeSession(bundle.sessionId)}
                  aria-label={`Close ${name}`}
                  title="Close this session"
                  className="text-muted hover:text-loss"
                >
                  &times;
                </button>
              )}
            </span>
          );
        })}
        {adding && <span className="text-xs text-muted">Loading session...</span>}
      </div>

      {sectors.length < sectorCount && active.segments.length > 0 && (
        <p className="mb-2 text-xs text-muted">
          This track segments into {active.segments.length} corners and straights, so {sectorCount}{" "}
          sectors would split one. Showing {sectors.length}.
        </p>
      )}

      {error && (
        <p className="mb-3 text-sm text-loss" role="alert">
          {error}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <div className="overflow-x-auto rounded border border-hairline bg-surface">
          <div className="min-w-[900px]">
            <div
              className="grid gap-2 border-b border-hairline px-3 py-2"
              style={{ gridTemplateColumns: columns }}
            >
              <span className="label" title="Include in analysis">
                Use
              </span>
              <span className="label" title="Add to comparison">
                Cmp
              </span>
              <span className="label">Lap</span>
              <span className="label text-right">Time</span>
              {sectors.map((sector) => (
                // Centred over its column, and in the colour that stretch of
                // tarmac is drawn in on the track map beside it -- so "S3" in
                // the table and the green stretch on the map are visibly the
                // same thing without having to count.
                <span
                  key={sector.index}
                  className="label text-center"
                  style={{ color: sectorColor(sector.index) }}
                  title={`${Math.round(sector.startM)}–${Math.round(sector.endM)} m`}
                >
                  S{sector.index + 1}
                </span>
              ))}
              <span className="label text-right">Delta</span>
              <span className="label text-right">Max</span>
            </div>

            <div
              className="grid gap-2 border-b border-hairline bg-theoretical/10 px-3 py-2"
              style={{ gridTemplateColumns: columns }}
            >
              <span />
              <span />
              <span className="text-xs font-semibold text-theoretical">Theo</span>
              <span className="text-right font-mono text-xs font-bold text-theoretical">
                {lapClock(stats.theoretical.total)}
              </span>
              {fastestPerSector.map((best, index) => (
                <span key={index} className="text-right font-mono text-xs text-theoretical">
                  {splitClock(best)}
                </span>
              ))}
              {/* The theoretical best is by definition at or under the best
                  actual lap, so this delta is negative or zero -- shown in the
                  gain colour, in the same mono face as the splits it is read
                  against. */}
              <span
                className={`text-right font-mono text-xs ${
                  stats.gain !== null && stats.gain < 0 ? "text-gain" : "text-muted"
                }`}
              >
                {stats.gain !== null ? deltaText(stats.gain) : ""}
              </span>
              <span />
            </div>

            {included.length === 0 ? (
              <p className="px-3 py-6 text-sm text-muted">
                Every lap in this session is excluded. Re-include one below to see splits.
              </p>
            ) : (
              included.map(({ row, sectors: times }) => (
                <LapLine
                  key={row.lapNumber}
                  row={row}
                  times={times}
                  columns={columns}
                  fastestPerSector={fastestPerSector}
                  bestLapTime={stats.bestLapTime}
                  compared={compared.has(lapKey(active.sessionId, row.lapNumber))}
                  onCompare={() => toggleCompared(active.sessionId, row.lapNumber)}
                  onToggleExcluded={active.canEdit ? () => toggleExcluded(row) : undefined}
                  busy={busy === row.lapNumber}
                />
              ))
            )}
          </div>
        </div>

        <TrackMap
          trace={active.trace}
          sectors={sectors}
          sectorTimes={fastestPerSector}
          formatTime={splitClock}
        />
      </div>

      {excluded.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowExcluded(!showExcluded)}
            className="text-sm text-muted underline hover:text-ink"
          >
            {showExcluded ? "Hide" : "Show"} excluded laps ({excluded.length})
          </button>

          {showExcluded && (
            <div className="mt-2 overflow-x-auto rounded border border-hairline bg-surface">
              <div className="min-w-[900px]">
                {excluded.map(({ row, sectors: times }) => (
                  <LapLine
                    key={row.lapNumber}
                    row={row}
                    times={times}
                    columns={columns}
                    fastestPerSector={fastestPerSector}
                    bestLapTime={stats.bestLapTime}
                    compared={compared.has(lapKey(active.sessionId, row.lapNumber))}
                    onCompare={() => toggleCompared(active.sessionId, row.lapNumber)}
                    onToggleExcluded={active.canEdit ? () => toggleExcluded(row) : undefined}
                    busy={busy === row.lapNumber}
                    dimmed
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {compared.size > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-3 rounded border border-hairline bg-raised px-4 py-3">
          <span className="text-sm">
            {compared.size} lap{compared.size === 1 ? "" : "s"} selected
            {multiSession ? " across all tabs" : ""}:
          </span>
          <span className="flex flex-wrap gap-2 text-xs">
            {traces.map((lap) => (
              <span key={lap.key} className="rounded bg-selected px-2 py-0.5 font-mono text-ink2">
                {lap.label}
              </span>
            ))}
          </span>
          <button
            type="button"
            onClick={() => setCompared(new Set())}
            className="ml-auto text-sm text-muted underline"
          >
            Clear
          </button>
        </div>
      )}

      <div className="mt-6">
        <LapCharts
          laps={traces}
          sectors={sectors}
          segments={active.segments}
          trace={active.trace}
          referenceKey={referenceKey}
          onReferenceChange={setReferenceKey}
        />
      </div>

      {dialog && (
        <AddSessionsDialog
          scope={dialog}
          trackName={initial.trackName}
          myProfileId={myProfileId}
          excludeSessionIds={bundles.map((b) => b.sessionId)}
          onCancel={() => setDialog(null)}
          onAdd={addSessions}
        />
      )}
    </div>
  );
}

function LapLine({
  row,
  times,
  columns,
  fastestPerSector,
  bestLapTime,
  compared,
  onCompare,
  onToggleExcluded,
  busy,
  dimmed,
}: {
  row: LapRow;
  times: (number | null)[];
  columns: string;
  fastestPerSector: (number | null)[];
  bestLapTime: number | null;
  compared: boolean;
  onCompare: () => void;
  onToggleExcluded?: () => void;
  busy: boolean;
  dimmed?: boolean;
}) {
  const excluded = row.isOutlier || row.excludedByUser;
  const isBest =
    bestLapTime !== null && row.lapTimeS !== null && Math.abs(row.lapTimeS - bestLapTime) < 0.0005;
  const delta = bestLapTime !== null && row.lapTimeS !== null ? row.lapTimeS - bestLapTime : null;

  return (
    <div
      className={`grid items-center gap-2 border-b border-hairline/60 px-3 py-1.5 hover:bg-rowalt ${
        dimmed ? "opacity-55" : ""
      } ${compared ? "bg-selected" : ""}`}
      style={{ gridTemplateColumns: columns }}
    >
      <span>
        {onToggleExcluded ? (
          <input
            type="checkbox"
            checked={!excluded}
            disabled={busy || row.isOutlier}
            onChange={onToggleExcluded}
            title={
              row.isOutlier
                ? `Automatically excluded: ${row.outlierReason ?? "outlier"}`
                : "Include this lap in the analysis"
            }
            className="h-3.5 w-3.5 accent-accent disabled:opacity-40"
          />
        ) : (
          <span className="text-[11px] text-muted">{excluded ? "—" : "•"}</span>
        )}
      </span>

      <span>
        <input
          type="checkbox"
          checked={compared}
          onChange={onCompare}
          title="Add this lap to the comparison"
          className="h-3.5 w-3.5 accent-reference"
        />
      </span>

      <span className="font-mono text-xs">{row.lapNumber}</span>

      <span
        className={`text-right font-mono text-xs font-bold ${isBest ? "text-reference" : "text-ink"}`}
      >
        {lapClock(row.lapTimeS)}
      </span>

      {times.map((time, index) => {
        const fastest =
          time !== null &&
          fastestPerSector[index] !== null &&
          Math.abs(time - (fastestPerSector[index] as number)) < 0.0005;
        return (
          <span
            key={index}
            className={`text-right font-mono text-xs ${fastest ? "text-reference" : "text-ink2"}`}
          >
            {splitClock(time)}
          </span>
        );
      })}

      <span
        className={`text-right font-mono text-xs ${
          delta === null || Math.abs(delta) < 0.0005 ? "text-muted" : "text-loss"
        }`}
      >
        {deltaText(delta)}
      </span>

      <span className="text-right font-mono text-[11px] text-muted">
        {row.maxSpeedKmh !== null ? `${Math.round(row.maxSpeedKmh)}` : "—"}
      </span>
    </div>
  );
}

// Written out rather than interpolated: Tailwind generates classes by
// scanning source text, so a `border-l-${tone}` template literal produces no
// CSS at all and the accent silently disappears in production.
const CARD_ACCENT = {
  ink: "border-l-ink2",
  loss: "border-l-loss",
  gain: "border-l-gain",
  reference: "border-l-reference",
  theoretical: "border-l-theoretical",
} as const;

function Card({
  label,
  tone,
  children,
}: {
  label: string;
  tone: keyof typeof CARD_ACCENT;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-md border border-hairline border-l-2 bg-raised px-4 py-3 ${CARD_ACCENT[tone]}`}
    >
      <div className="label mb-1">{label}</div>
      {children}
    </div>
  );
}

function Header({ bundle }: { bundle: SessionBundle }) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-4">
      <div className="rounded border border-hairline bg-surface px-3 py-2">
        <div className="text-sm font-semibold text-gain">
          <DriverName name={bundle.driverName} country={bundle.driverCountry} />
        </div>
        <div className="text-[11px] text-muted">
          {sessionDate(bundle.startDate)} {sessionTime(bundle.startTime)}
          {bundle.trackName ? ` · ${bundle.trackName}` : ""}
        </div>
      </div>
      {[bundle.kartClass, bundle.trackCondition].filter(Boolean).map((badge) => (
        <span
          key={badge}
          className="rounded bg-selected px-2 py-1 text-[9px] font-semibold uppercase tracking-wider text-ink2"
        >
          {badge}
        </span>
      ))}
      <div className="ml-auto flex items-center gap-3 text-sm">
        <span className="border-b-2 border-accent pb-0.5 font-semibold">Lap analysis</span>
        <Link
          href={`/sessions/${bundle.sessionId}/engine`}
          className="text-muted underline hover:text-ink"
        >
          Engine analysis
        </Link>
        <Link href="/" className="text-muted underline hover:text-ink">
          All sessions
        </Link>
      </div>
    </div>
  );
}
