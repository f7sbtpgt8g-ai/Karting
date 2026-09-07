import Link from "next/link";
import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import { buildSectors, sectorTimes, DEFAULT_SECTORS, type Segment } from "@/lib/sectors";
import type { TracePoint } from "@/lib/trackMap";
import TrackDetailClient from "./TrackDetailClient";
import type { DriverPodiumRow, MyBestRow, TeamPodiumRow } from "@/lib/tracks";

export const dynamic = "force-dynamic";

/**
 * One track's leaderboards and map.
 *
 * The four RPCs this calls (0011_track_leaderboards.sql) do the real work:
 * `track_my_best` rides RLS as-is (it's the caller's own data, whatever
 * their own visibility lets them see), while `track_driver_podium` and
 * `track_team_podium` explicitly repeat a public-only predicate so the
 * podium is identical for every viewer regardless of team membership --
 * that guarantee lives in the database, not here.
 */
export default async function TrackDetailPage({ params }: { params: { trackName: string } }) {
  const trackName = params.trackName;

  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

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

  const [myBestRes, driverPodiumRes, teamPodiumRes, mapSourceRes] = await Promise.all([
    supabase.rpc("track_my_best", { p_track_name: trackName, p_engine_category: null }),
    supabase.rpc("track_driver_podium", { p_track_name: trackName, p_engine_category: null, p_limit: 10 }),
    supabase.rpc("track_team_podium", { p_track_name: trackName, p_engine_category: null, p_limit: 3 }),
    supabase.rpc("track_map_source", { p_track_name: trackName, p_engine_category: null }),
  ]);

  const rawMyBest = ((myBestRes.data as RawMyBest[] | null) ?? [])[0] ?? null;
  const myBest: MyBestRow | null = rawMyBest && {
    bestLapS: rawMyBest.best_lap_s,
    averageLapS: rawMyBest.average_lap_s,
    sessionCount: rawMyBest.session_count,
    lastDrivenDate: rawMyBest.last_driven_date,
  };

  const driverPodium: DriverPodiumRow[] = ((driverPodiumRes.data as RawDriverPodium[] | null) ?? []).map(
    (row) => ({
      rank: row.rank,
      driverProfileId: row.driver_profile_id,
      driverName: row.driver_name,
      driverCountry: row.driver_country,
      bestLapS: row.best_lap_s,
      engineCategory: row.engine_category,
      teamName: row.team_name,
    }),
  );

  const teamPodium: TeamPodiumRow[] = ((teamPodiumRes.data as RawTeamPodium[] | null) ?? []).map((row) => ({
    rank: row.rank,
    teamId: row.team_id,
    teamName: row.team_name,
    bestLapS: row.best_lap_s,
    fastestDriverName: row.fastest_driver_name,
    fastestDriverCountry: row.fastest_driver_country,
  }));

  const mapSource = ((mapSourceRes.data as RawMapSource[] | null) ?? [])[0] ?? null;

  // The map needs one lap's shape, not the whole session's telemetry --
  // the same three reads `sessions/[id]/page.tsx` makes for the same
  // reason, reused rather than reinvented.
  let trace: TracePoint[] = [];
  let sectors: ReturnType<typeof buildSectors> = [];
  let sectorTimeValues: (number | null)[] = [];

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

    const segments = analysis?.segments ?? [];
    sectors = buildSectors(segments, DEFAULT_SECTORS);
    const timesByLabel = new Map<string, number | null>(
      (segmentTimeRows ?? [])
        .filter((row): row is { segment_label: string; time_s: number | null } => Boolean(row.segment_label))
        .map((row) => [row.segment_label, row.time_s]),
    );
    sectorTimeValues = sectorTimes(sectors, segments, timesByLabel);

    trace =
      traceRow?.latitude && traceRow.longitude && traceRow.distance_m
        ? traceRow.distance_m.map((distanceM, i) => ({
            lat: traceRow.latitude![i],
            lon: traceRow.longitude![i],
            distanceM,
          }))
        : [];
  }

  return (
    <main className="mx-auto max-w-[1400px] px-6 py-8">
      <AppHeader email={appUser.email} current="/tracks" isAdmin={appUser?.is_admin} />
      <Link href="/tracks" className="mb-2 inline-block text-xs text-muted underline">
        &larr; All tracks
      </Link>
      <h1 className="mb-6 text-lg font-semibold">{trackName}</h1>
      <TrackDetailClient
        trackName={trackName}
        initialMyBest={myBest}
        initialDriverPodium={driverPodium}
        initialTeamPodium={teamPodium}
        initialTrace={trace}
        initialSectors={sectors}
        initialSectorTimes={sectorTimeValues}
      />
    </main>
  );
}
