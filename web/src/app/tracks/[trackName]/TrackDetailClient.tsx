"use client";

import { useEffect, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { ENGINE_CATEGORIES, engineColor } from "@/lib/engine";
import { lapTime } from "@/lib/format";
import { DriverName } from "@/components/CountryFlag";
import {
  buildSectors,
  sectorTimes as computeSectorTimes,
  DEFAULT_SECTORS,
  type Sector,
  type Segment,
} from "@/lib/sectors";
import type { TracePoint } from "@/lib/trackMap";
import TrackMap from "@/app/sessions/[id]/TrackMap";
import {
  podiumPlaceColor,
  type DriverPodiumRow,
  type MyBestRow,
  type TeamPodiumRow,
} from "@/lib/tracks";

type RawMyBest = {
  best_lap_s: number | null;
  average_lap_s: number | null;
  session_count: number;
  last_driven_date: string | null;
};
type RawDriverPodium = {
  rank: number;
  driver_profile_id: number;
  driver_name: string;
  driver_country: string | null;
  best_lap_s: number;
  engine_category: string | null;
  team_name: string | null;
};
type RawTeamPodium = {
  rank: number;
  team_id: number;
  team_name: string;
  best_lap_s: number;
  fastest_driver_name: string;
  fastest_driver_country: string | null;
};
type RawMapSource = { session_id: number | null; lap_number: number | null };

export default function TrackDetailClient({
  trackName,
  initialMyBest,
  initialDriverPodium,
  initialTeamPodium,
  initialTrace,
  initialSectors,
  initialSectorTimes,
}: {
  trackName: string;
  initialMyBest: MyBestRow | null;
  initialDriverPodium: DriverPodiumRow[];
  initialTeamPodium: TeamPodiumRow[];
  initialTrace: TracePoint[];
  initialSectors: Sector[];
  initialSectorTimes: (number | null)[];
}) {
  const [engineCategory, setEngineCategory] = useState("");
  const [myBest, setMyBest] = useState(initialMyBest);
  const [driverPodium, setDriverPodium] = useState(initialDriverPodium);
  const [teamPodium, setTeamPodium] = useState(initialTeamPodium);
  const [trace, setTrace] = useState(initialTrace);
  const [sectors, setSectors] = useState(initialSectors);
  const [sectorTimeValues, setSectorTimeValues] = useState(initialSectorTimes);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Skips its first run: the server already fetched exactly this (the
  // unfiltered view) on first paint, so re-requesting it on mount would
  // just be a flicker.
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    let cancelled = false;

    async function load() {
      setBusy(true);
      setError(null);
      const supabase = createClient();
      const params = { p_track_name: trackName, p_engine_category: engineCategory || null };
      const [myBestRes, driverRes, teamRes, mapRes] = await Promise.all([
        supabase.rpc("track_my_best", params),
        supabase.rpc("track_driver_podium", { ...params, p_limit: 10 }),
        supabase.rpc("track_team_podium", { ...params, p_limit: 3 }),
        supabase.rpc("track_map_source", params),
      ]);
      if (cancelled) return;

      const rpcError = myBestRes.error ?? driverRes.error ?? teamRes.error ?? mapRes.error;
      if (rpcError) {
        setBusy(false);
        setError(rpcError.message);
        return;
      }

      const rawBest = ((myBestRes.data as RawMyBest[] | null) ?? [])[0] ?? null;
      setMyBest(
        rawBest && {
          bestLapS: rawBest.best_lap_s,
          averageLapS: rawBest.average_lap_s,
          sessionCount: rawBest.session_count,
          lastDrivenDate: rawBest.last_driven_date,
        },
      );

      setDriverPodium(
        ((driverRes.data as RawDriverPodium[] | null) ?? []).map((row) => ({
          rank: row.rank,
          driverProfileId: row.driver_profile_id,
          driverName: row.driver_name,
          driverCountry: row.driver_country,
          bestLapS: row.best_lap_s,
          engineCategory: row.engine_category,
          teamName: row.team_name,
        })),
      );

      setTeamPodium(
        ((teamRes.data as RawTeamPodium[] | null) ?? []).map((row) => ({
          rank: row.rank,
          teamId: row.team_id,
          teamName: row.team_name,
          bestLapS: row.best_lap_s,
          fastestDriverName: row.fastest_driver_name,
          fastestDriverCountry: row.fastest_driver_country,
        })),
      );

      const mapSource = ((mapRes.data as RawMapSource[] | null) ?? [])[0] ?? null;

      if (mapSource?.session_id && mapSource.lap_number) {
        const [{ data: analysis }, { data: traceRow }, { data: segmentTimeRows }] = await Promise.all([
          supabase
            .from("session_analysis")
            .select("segments")
            .eq("session_db_id", mapSource.session_id)
            .maybeSingle()
            .returns<{ segments: Segment[] | null }>(),
          supabase
            .from("lap_traces")
            .select("latitude, longitude, distance_m")
            .eq("session_db_id", mapSource.session_id)
            .eq("lap_number", mapSource.lap_number)
            .maybeSingle()
            .returns<{ latitude: number[] | null; longitude: number[] | null; distance_m: number[] | null }>(),
          supabase
            .from("lap_segment_times")
            .select("segment_label, time_s")
            .eq("session_db_id", mapSource.session_id)
            .eq("lap_number", mapSource.lap_number)
            .returns<{ segment_label: string | null; time_s: number | null }[]>(),
        ]);
        if (cancelled) return;

        const segments = analysis?.segments ?? [];
        const newSectors = buildSectors(segments, DEFAULT_SECTORS);
        const timesByLabel = new Map<string, number | null>(
          (segmentTimeRows ?? [])
            .filter((row): row is { segment_label: string; time_s: number | null } =>
              Boolean(row.segment_label),
            )
            .map((row) => [row.segment_label, row.time_s]),
        );
        setSectors(newSectors);
        setSectorTimeValues(computeSectorTimes(newSectors, segments, timesByLabel));
        setTrace(
          traceRow?.latitude && traceRow.longitude && traceRow.distance_m
            ? traceRow.distance_m.map((distanceM, i) => ({
                lat: traceRow.latitude![i],
                lon: traceRow.longitude![i],
                distanceM,
              }))
            : [],
        );
      } else {
        setSectors([]);
        setSectorTimeValues([]);
        setTrace([]);
      }

      setBusy(false);
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [engineCategory, trackName]);

  return (
    <div>
      <label className="mb-6 block max-w-xs">
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

      <div className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <div className="space-y-6">
          {myBest && myBest.bestLapS !== null && (
            <div className="rounded border border-accent/40 bg-raised p-4">
              <div className="label mb-1">Your best</div>
              <div className="flex items-baseline gap-3">
                <span className="font-mono text-2xl font-bold">{lapTime(myBest.bestLapS)}</span>
                <span className="text-xs text-muted">
                  {myBest.sessionCount} session{myBest.sessionCount === 1 ? "" : "s"} &middot; avg{" "}
                  {lapTime(myBest.averageLapS)}
                </span>
              </div>
            </div>
          )}

          <section>
            <h2 className="label mb-2">All drivers</h2>
            {driverPodium.length === 0 ? (
              <p className="text-sm text-muted">No public laps at this track/class yet.</p>
            ) : (
              <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
                {driverPodium.map((row) => (
                  <div key={row.driverProfileId} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                    <span
                      className="w-5 text-right font-mono font-bold"
                      style={{ color: podiumPlaceColor(row.rank) }}
                    >
                      {row.rank}
                    </span>
                    <span className="flex-1 font-semibold">
                      <DriverName name={row.driverName} country={row.driverCountry} />
                    </span>
                    {row.teamName && <span className="text-xs text-muted">{row.teamName}</span>}
                    {row.engineCategory && (
                      <span
                        className="rounded bg-selected px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ink2"
                        style={{ color: engineColor(row.engineCategory) ?? undefined }}
                      >
                        {row.engineCategory}
                      </span>
                    )}
                    <span className="font-mono text-xs font-bold">{lapTime(row.bestLapS)}</span>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section>
            <h2 className="label mb-2">Team podium</h2>
            {teamPodium.length === 0 ? (
              <p className="text-sm text-muted">No team has a public lap at this track/class yet.</p>
            ) : (
              <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
                {teamPodium.map((row) => (
                  <div key={row.teamId} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                    <span
                      className="w-5 text-right font-mono font-bold"
                      style={{ color: podiumPlaceColor(row.rank) }}
                    >
                      {row.rank}
                    </span>
                    <span className="flex-1 font-semibold">{row.teamName}</span>
                    <span className="text-xs text-muted">
                      <DriverName name={row.fastestDriverName} country={row.fastestDriverCountry} />
                    </span>
                    <span className="font-mono text-xs font-bold">{lapTime(row.bestLapS)}</span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>

        <TrackMap trace={trace} sectors={sectors} sectorTimes={sectorTimeValues} formatTime={lapTime} />
      </div>
    </div>
  );
}
