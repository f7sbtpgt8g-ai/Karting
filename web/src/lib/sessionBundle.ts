/**
 * Everything Lap Analysis needs about one session, fetched in one place.
 *
 * The page loads its own session on the server; sessions added for comparison
 * are loaded in the browser, after the page is already up. Both need exactly
 * the same shape, so the shape is defined once here and the two loaders
 * agree by construction rather than by being written twice and kept in step.
 *
 * Every query goes through the ordinary anon client, so RLS is what decides
 * whether another driver's session can be read at all. There is no
 * "comparison" permission to get wrong: if `sessions_select` does not return
 * the row, none of the follow-up queries return anything either.
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import { driverNameWithFlag } from "./flags";
import type { Segment } from "./sectors";
import type { TracePoint } from "./trackMap";

export type LapRow = {
  lapNumber: number;
  lapTimeS: number | null;
  isOutlier: boolean;
  outlierReason: string | null;
  excludedByUser: boolean;
  maxSpeedKmh: number | null;
  maxRpm: number | null;
  segmentTimes: Record<string, number | null>;
};

export type SessionBundle = {
  sessionId: number;
  driverName: string;
  trackName: string | null;
  startDate: string | null;
  startTime: string | null;
  kartClass: string | null;
  trackCondition: string | null;
  segments: Segment[];
  speedIsEstimated: boolean;
  dataError: string | null;
  laps: LapRow[];
  /** Whether this viewer may exclude laps -- mirrors `laps_update_own`. */
  canEdit: boolean;
  /** The best lap's positions, for the track map. */
  trace: TracePoint[];
  peaksMissing: boolean;
};

type RawSession = {
  id: number;
  track_name: string | null;
  session_type: string | null;
  start_date: string | null;
  start_time: string | null;
  track_condition: string | null;
  kart_class: string | null;
  driver_profile_id: number | null;
  uploaded_by_user_id: number | null;
  driver_profiles: { display_name: string; user_id: number | null } | null;
};

type RawAnalysis = {
  best_lap: number | null;
  theoretical_best_s: number | null;
  speed_is_estimated: boolean;
  segments: Segment[] | null;
  summary: Record<string, number | null> | null;
  data_error: string | null;
};

/** The columns Lap Analysis reads off `sessions`, shared by both loaders. */
export const SESSION_COLUMNS =
  "id, track_name, session_type, start_date, start_time, track_condition, " +
  "kart_class, driver_profile_id, uploaded_by_user_id, " +
  "driver_profiles(display_name, user_id)";

/**
 * Assemble the bundle from rows already fetched.
 *
 * Split out from the fetching so the server component can reuse it for the
 * session it loaded itself, and so this -- the part with actual logic in it,
 * joining three result sets by lap number -- is testable without a database.
 */
export function buildBundle({
  session,
  analysis,
  laps,
  segmentTimes,
  peaks,
  trace,
  appUserId,
  driverCountry,
}: {
  session: RawSession;
  analysis: RawAnalysis | null;
  laps: {
    lap_number: number;
    lap_time_s: number | null;
    is_outlier: boolean | null;
    outlier_reason: string | null;
    excluded_by_user: boolean;
  }[];
  segmentTimes: { lap_number: number; segment_label: string | null; time_s: number | null }[];
  peaks: { lap_number: number; max_speed_kmh: number | null; max_rpm: number | null }[];
  trace: { latitude: number[] | null; longitude: number[] | null; distance_m: number[] | null } | null;
  appUserId: number | null;
  /** users.country isn't readable cross-driver via plain RLS, so the caller
   *  fetches it separately via the driver_country() RPC (0014) and passes
   *  it in here, rather than this function reaching for a client itself. */
  driverCountry?: string | null;
}): SessionBundle {
  const peakByLap = new Map(peaks.map((p) => [p.lap_number, p]));

  const timesByLap = new Map<number, Record<string, number | null>>();
  for (const row of segmentTimes) {
    if (!row.segment_label) continue;
    const bucket = timesByLap.get(row.lap_number) ?? {};
    bucket[row.segment_label] = row.time_s;
    timesByLap.set(row.lap_number, bucket);
  }

  const rows: LapRow[] = laps.map((lap) => ({
    lapNumber: lap.lap_number,
    lapTimeS: lap.lap_time_s,
    isOutlier: Boolean(lap.is_outlier),
    outlierReason: lap.outlier_reason,
    excludedByUser: Boolean(lap.excluded_by_user),
    maxSpeedKmh: peakByLap.get(lap.lap_number)?.max_speed_kmh ?? null,
    maxRpm: peakByLap.get(lap.lap_number)?.max_rpm ?? null,
    segmentTimes: timesByLap.get(lap.lap_number) ?? {},
  }));

  const points: TracePoint[] =
    trace?.latitude && trace.longitude && trace.distance_m
      ? trace.distance_m.map((distanceM, i) => ({
          lat: trace.latitude![i],
          lon: trace.longitude![i],
          distanceM,
        }))
      : [];

  return {
    sessionId: session.id,
    driverName: driverNameWithFlag(
      session.driver_profiles?.display_name ?? session.track_name ?? "Session",
      driverCountry ?? null,
    ),
    trackName: session.track_name,
    startDate: session.start_date,
    startTime: session.start_time,
    kartClass: session.kart_class,
    trackCondition: session.track_condition,
    segments: analysis?.segments ?? [],
    speedIsEstimated: Boolean(analysis?.speed_is_estimated),
    dataError: analysis?.data_error ?? null,
    laps: rows,
    // The same rule `laps_update_own` enforces in the database, checked here
    // too so the toggles are simply absent on a session you are only looking
    // at rather than present and failing.
    canEdit:
      appUserId != null &&
      (session.uploaded_by_user_id === appUserId ||
        session.driver_profiles?.user_id === appUserId),
    trace: points,
    peaksMissing: rows.length > 0 && rows.every((r) => r.maxSpeedKmh === null),
  };
}

/**
 * Load one session in the browser.
 *
 * Returns null when the session has no analysis stored: it can be listed and
 * still have nothing to compare, and an empty tab is a worse answer than
 * saying so where the session was picked.
 */
export async function loadSessionBundle(
  supabase: SupabaseClient,
  sessionId: number,
  appUserId: number | null,
): Promise<SessionBundle | null> {
  const { data: session } = await supabase
    .from("sessions")
    .select(SESSION_COLUMNS)
    .eq("id", sessionId)
    .maybeSingle()
    .returns<RawSession>();
  if (!session) return null;

  const [{ data: analysis }, { data: laps }, { data: segmentTimes }, { data: peaks }] =
    await Promise.all([
      supabase
        .from("session_analysis")
        .select("best_lap, theoretical_best_s, speed_is_estimated, segments, summary, data_error")
        .eq("session_db_id", sessionId)
        .maybeSingle()
        .returns<RawAnalysis>(),
      supabase
        .from("laps")
        .select("lap_number, lap_time_s, is_outlier, outlier_reason, excluded_by_user")
        .eq("session_db_id", sessionId)
        .order("lap_number")
        .returns<
          {
            lap_number: number;
            lap_time_s: number | null;
            is_outlier: boolean | null;
            outlier_reason: string | null;
            excluded_by_user: boolean;
          }[]
        >(),
      supabase
        .from("lap_segment_times")
        .select("lap_number, segment_label, time_s")
        .eq("session_db_id", sessionId)
        .returns<{ lap_number: number; segment_label: string | null; time_s: number | null }[]>(),
      supabase
        .from("lap_traces")
        .select("lap_number, max_speed_kmh, max_rpm")
        .eq("session_db_id", sessionId)
        .returns<{ lap_number: number; max_speed_kmh: number | null; max_rpm: number | null }[]>(),
    ]);

  if (!analysis) return null;

  const { data: trace } = analysis.best_lap
    ? await supabase
        .from("lap_traces")
        .select("latitude, longitude, distance_m")
        .eq("session_db_id", sessionId)
        .eq("lap_number", analysis.best_lap)
        .maybeSingle()
        .returns<{
          latitude: number[] | null;
          longitude: number[] | null;
          distance_m: number[] | null;
        }>()
    : { data: null };

  const { data: driverCountry } = session.driver_profile_id
    ? await supabase.rpc("driver_country", { p_driver_profile_id: session.driver_profile_id })
    : { data: null };

  return buildBundle({
    session,
    analysis,
    laps: laps ?? [],
    driverCountry: driverCountry as string | null,
    segmentTimes: segmentTimes ?? [],
    peaks: peaks ?? [],
    trace,
    appUserId,
  });
}
