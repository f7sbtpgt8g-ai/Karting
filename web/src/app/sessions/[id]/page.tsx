import Link from "next/link";
import { createClient, getAppUser } from "@/lib/supabase/server";
import AppHeader from "@/components/AppHeader";
import { SESSION_COLUMNS, buildBundle } from "@/lib/sessionBundle";
import type { Segment } from "@/lib/sectors";
import LapAnalysis from "./LapAnalysis";

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
 * Lap Analysis for one session, plus any others opened alongside it.
 *
 * Everything here comes from the tables 0005 added rather than from the
 * Parquet blob -- which is the whole reason this page can exist at all in a
 * browser. The heavy per-sample arrays in `lap_traces` are deliberately not
 * selected: this page needs per-lap scalars and per-segment times, and
 * pulling ~300 KB of traces to render a table would be paying for the charts
 * before they are built.
 *
 * Sessions added for comparison are fetched in the browser through the same
 * `buildBundle`, so a teammate's session arrives shaped exactly like this
 * one. RLS is what decides whether it arrives at all.
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
    .select(SESSION_COLUMNS)
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

  // Only the best lap's positions, and only three of its eight arrays: the
  // track map needs one lap's shape, not the whole session's telemetry.
  const { data: bestTrace } = analysis?.best_lap
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

  if (!analysis) {
    return (
      <main className="mx-auto max-w-6xl px-6 py-8">
        <AppHeader email={appUser?.email} current="/" isAdmin={appUser?.is_admin} />
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

  // The viewer's own driver profile, which decides what "my team" and "my
  // sessions" mean in the add-session searches.
  const { data: myProfile } = appUser
    ? await supabase
        .from("driver_profiles")
        .select("id")
        .eq("user_id", appUser.id)
        .maybeSingle()
    : { data: null };

  const bundle = buildBundle({
    session,
    analysis,
    laps: laps ?? [],
    segmentTimes: segmentTimes ?? [],
    peaks: peaks ?? [],
    trace: bestTrace,
    appUserId: appUser?.id ?? null,
  });

  return (
    <main className="mx-auto max-w-[1400px] px-6 py-8">
      <AppHeader email={appUser?.email} current="/" isAdmin={appUser?.is_admin} />
      <LapAnalysis
        initial={bundle}
        appUserId={appUser?.id ?? null}
        myProfileId={(myProfile?.id as number | undefined) ?? null}
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
