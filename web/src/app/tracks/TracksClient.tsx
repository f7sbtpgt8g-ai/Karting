"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { createClient } from "@/lib/supabase/client";
import { ENGINE_CATEGORIES, engineColor } from "@/lib/engine";
import { lapTime, sessionDate } from "@/lib/format";
import { driverNameWithFlag } from "@/lib/flags";
import type { TrackSummaryRow } from "@/lib/tracks";

/**
 * One definition of the column layout, used by the header and every row --
 * the same idiom `HomeClient.tsx`'s `COLUMNS` constant follows, so a header
 * never drifts out from over its own data.
 */
const COLUMNS = "grid grid-cols-[2fr_0.8fr_1fr_1.6fr_1fr_1fr] items-center gap-2";

type SortKey = "trackName" | "bestLapS" | "lastDrivenDate";

export default function TracksClient({ tracks }: { tracks: TrackSummaryRow[] }) {
  const [rows, setRows] = useState(tracks);
  const [engineCategory, setEngineCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("trackName");
  const [sortDesc, setSortDesc] = useState(false);

  // Re-fetch from the RPC whenever the class filter changes -- the server
  // component only ever renders the unfiltered ("All classes") view. Skips
  // its first run: the server already fetched exactly this on first paint,
  // and re-requesting it immediately on mount would just be a flicker.
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    let cancelled = false;
    setBusy(true);
    setError(null);
    createClient()
      .rpc("track_summaries", { p_engine_category: engineCategory || null })
      .then(({ data, error: rpcError }) => {
        if (cancelled) return;
        setBusy(false);
        if (rpcError) {
          setError(rpcError.message);
          return;
        }
        type RawRow = {
          track_name: string;
          session_count: number;
          best_lap_s: number | null;
          best_lap_driver_name: string | null;
          best_lap_driver_country: string | null;
          best_lap_engine_category: string | null;
          average_lap_s: number | null;
          last_driven_date: string | null;
        };
        setRows(
          ((data ?? []) as RawRow[]).map((row) => ({
            trackName: row.track_name,
            sessionCount: row.session_count,
            bestLapS: row.best_lap_s,
            bestLapDriverName: row.best_lap_driver_name,
            bestLapDriverCountry: row.best_lap_driver_country,
            bestLapEngineCategory: row.best_lap_engine_category,
            averageLapS: row.average_lap_s,
            lastDrivenDate: row.last_driven_date,
          })),
        );
      });
    return () => {
      cancelled = true;
    };
  }, [engineCategory]);

  const stats = useMemo(
    () => [
      ["Tracks", rows.length],
      ["Sessions", rows.reduce((sum, r) => sum + r.sessionCount, 0)],
    ],
    [rows],
  );

  const sorted = useMemo(() => {
    const copy = [...rows];
    copy.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "trackName") cmp = a.trackName.localeCompare(b.trackName);
      else if (sortKey === "bestLapS") cmp = (a.bestLapS ?? Infinity) - (b.bestLapS ?? Infinity);
      else cmp = (a.lastDrivenDate ?? "").localeCompare(b.lastDrivenDate ?? "");
      return sortDesc ? -cmp : cmp;
    });
    return copy;
  }, [rows, sortKey, sortDesc]);

  function SortHeader({ label, column }: { label: string; column: SortKey }) {
    const active = sortKey === column;
    return (
      <button
        type="button"
        onClick={() => {
          if (active) setSortDesc(!sortDesc);
          else {
            setSortKey(column);
            setSortDesc(false);
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

      <label className="mb-4 block max-w-xs">
        <span className="label mb-1 block">Engine class</span>
        <select
          value={engineCategory}
          onChange={(e) => setEngineCategory(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-2 py-1.5 text-sm"
        >
          <option value="">All classes</option>
          {ENGINE_CATEGORIES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>

      {error && (
        <p className="mb-4 rounded border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss">{error}</p>
      )}

      <div className="overflow-x-auto">
        <div className="min-w-[720px]">
          <div className={`${COLUMNS} border-b border-hairline pb-1`}>
            <SortHeader label="Track" column="trackName" />
            <span className="label">Sessions</span>
            <SortHeader label="Best lap" column="bestLapS" />
            <span className="label">Best by</span>
            <span className="label">Average lap</span>
            <SortHeader label="Last driven" column="lastDrivenDate" />
          </div>

          {sorted.length === 0 && !busy ? (
            <p className="py-6 text-sm text-muted">No tracks match this class yet.</p>
          ) : (
            sorted.map((row) => (
              <Link
                key={row.trackName}
                href={`/tracks/${encodeURIComponent(row.trackName)}`}
                className={`${COLUMNS} border-b border-hairline/60 py-2 text-sm hover:bg-rowalt`}
              >
                <span className="font-semibold text-ink">{row.trackName}</span>
                <span className="text-xs text-muted">{row.sessionCount}</span>
                <span className="font-mono text-xs font-bold">{lapTime(row.bestLapS)}</span>
                <span className="flex items-center gap-1.5 text-xs text-muted">
                  {row.bestLapDriverName
                    ? driverNameWithFlag(row.bestLapDriverName, row.bestLapDriverCountry)
                    : "--"}
                  {row.bestLapEngineCategory && (
                    <span
                      className="rounded bg-selected px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink2"
                      style={{ color: engineColor(row.bestLapEngineCategory) ?? undefined }}
                    >
                      {row.bestLapEngineCategory}
                    </span>
                  )}
                </span>
                <span className="font-mono text-xs text-muted">{lapTime(row.averageLapS)}</span>
                <span className="text-xs text-muted">
                  {row.lastDrivenDate ? sessionDate(row.lastDrivenDate) : "--"}
                </span>
              </Link>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
