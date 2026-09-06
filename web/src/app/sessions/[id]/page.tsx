import Link from "next/link";
import { createClient, getAppUser } from "@/lib/supabase/server";
import AppHeader from "@/components/AppHeader";
import type { Segment } from "@/lib/sectors";
import LapAnalysis, { type LapRow } from "./LapAnalysis";

export const dynamic = "force-dynamic";

type SessionRow = {
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

type AnalysisRow = {
  best_lap: number | null;
  theoretical_best_s: number | null;
  speed_is_estimated: boolean;
  segments: Segment[] | null;
  summary: Record<string, number | null> | null;
  data_error: string | null;
};

/**
 * Lap Analysis for one session.
 *
 * Everything here comes from the tables 0005 added rather than from the
 * Parquet blob -- which is the whole reason this page can exist at all in a
 * browser. The heavy per-sample arrays in `lap_traces` are deliberately not
 * selected: this page needs per-lap scalars and per-segment times, and
 * pulling ~300 KB of traces to render a table would be paying for the charts
 * before they are built.
 */
export default async function SessionPage({ params }: { params: { id: string } }) {
  const sessionId = Number(params.id);
  if (!Number.isFinite(sessionId)) {
    return <Missing />;
  }

  const appUser = await getAppUser();
  const supabase = await createClient();

  const { data: session } = await supabase
    .from("sessions")
    .select(
      "id, track_name, session_type, start_date, start_time, track_condition, " +
        "kart_class, driver_profile_id, uploaded_by_user_id, " +
        "driver_profiles(display_name, user_id)",
    )
    .eq("id", sessionId)
    .maybeSingle()
    .returns<SessionRow>();

  // RLS already decided this: a session the caller may not see simply is not
  // returned, so there is no separate authorisation check to get wrong here.
  if (!session) return <Missing />;

  const [{ data: analysis }, { data: laps }, { data: segmentTimes }, { data: peaks }] =
    await Promise.all([
      supabase
        .from("session_analysis")
        .select("best_lap, theoretical_best_s, speed_is_estimated, segments, summary, data_error")
        .eq("session_db_id", sessionId)
        .maybeSingle()
        .returns<AnalysisRow>(),
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

  if (!analysis) {
    return (
      <main className="mx-auto max-w-6xl px-6 py-8">
        <AppHeader email={appUser?.email} current="/" />
        <h1 className="mb-2 text-lg font-semibold">Not analysed yet</h1>
        <p className="text-sm text-muted">
          This session is stored, but its analysis has not been computed. New uploads are analysed
          as they arrive; older sessions need{" "}
          <code className="text-ink2">scripts/backfill_analysis.py --analyze</code>.
        </p>
        <Link href="/" className="mt-6 inline-block text-sm text-muted underline">
          Back to Home
        </Link>
      </main>
    );
  }

  const peakByLap = new Map((peaks ?? []).map((p) => [p.lap_number, p]));
  const timesByLap = new Map<number, Record<string, number | null>>();
  for (const row of segmentTimes ?? []) {
    if (!row.segment_label) continue;
    const bucket = timesByLap.get(row.lap_number) ?? {};
    bucket[row.segment_label] = row.time_s;
    timesByLap.set(row.lap_number, bucket);
  }

  const rows: LapRow[] = (laps ?? []).map((lap) => ({
    lapNumber: lap.lap_number,
    lapTimeS: lap.lap_time_s,
    isOutlier: Boolean(lap.is_outlier),
    outlierReason: lap.outlier_reason,
    excludedByUser: Boolean(lap.excluded_by_user),
    maxSpeedKmh: peakByLap.get(lap.lap_number)?.max_speed_kmh ?? null,
    maxRpm: peakByLap.get(lap.lap_number)?.max_rpm ?? null,
    segmentTimes: timesByLap.get(lap.lap_number) ?? {},
  }));

  // Editable only by the session's own driver or its uploader -- the same
  // rule `laps_update_own` enforces in the database. Checked here too so the
  // toggles are simply absent for a teammate rather than present and failing.
  const canEdit =
    appUser != null &&
    (session.uploaded_by_user_id === appUser.id ||
      session.driver_profiles?.user_id === appUser.id);

  return (
    <main className="mx-auto max-w-[1400px] px-6 py-8">
      <AppHeader email={appUser?.email} current="/" />
      <LapAnalysis
        sessionId={sessionId}
        driverName={session.driver_profiles?.display_name ?? session.track_name ?? "Session"}
        trackName={session.track_name}
        startDate={session.start_date}
        startTime={session.start_time}
        kartClass={session.kart_class}
        trackCondition={session.track_condition}
        segments={analysis.segments ?? []}
        speedIsEstimated={analysis.speed_is_estimated}
        dataError={analysis.data_error}
        laps={rows}
        canEdit={canEdit}
      />
    </main>
  );
}

function Missing() {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="mb-3 text-lg font-semibold">Session not found</h1>
      <p className="text-sm text-muted">
        It may have been deleted, or belong to a driver who has not shared it with you.
      </p>
      <Link href="/" className="mt-6 inline-block text-sm text-muted underline">
        Back to Home
      </Link>
    </main>
  );
}
