import Link from "next/link";
import { createClient, getAppUser } from "@/lib/supabase/server";
import AppHeader from "@/components/AppHeader";
import EngineAnalysis, { type EngineLapRow } from "./EngineAnalysis";

export const dynamic = "force-dynamic";

/**
 * Engine analysis for one session: the same laps as Lap Analysis, read
 * through what the engine was doing rather than where the time went.
 *
 * A separate route rather than a tab on Lap Analysis because it answers a
 * different question and wants none of the same data -- no sectors, no
 * segment map, no traces. A tab would load both and show one.
 */
export default async function EnginePage({ params }: { params: { id: string } }) {
  const sessionId = Number(params.id);
  const appUser = await getAppUser();
  const supabase = await createClient();

  const { data: session } = await supabase
    .from("sessions")
    .select("id, track_name, start_date, start_time, driver_profiles(display_name)")
    .eq("id", sessionId)
    .maybeSingle()
    .returns<{
      id: number;
      track_name: string | null;
      start_date: string | null;
      start_time: string | null;
      driver_profiles: { display_name: string } | null;
    }>();

  if (!session) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-16">
        <h1 className="mb-3 text-lg font-semibold">Session not found</h1>
        <Link href="/" className="text-sm text-muted underline">
          Back to Home
        </Link>
      </main>
    );
  }

  const [{ data: laps }, { data: traces }] = await Promise.all([
    supabase
      .from("laps")
      .select("lap_number, lap_time_s, is_outlier, excluded_by_user")
      .eq("session_db_id", sessionId)
      .order("lap_number")
      .returns<
        {
          lap_number: number;
          lap_time_s: number | null;
          is_outlier: boolean | null;
          excluded_by_user: boolean;
        }[]
      >(),
    supabase
      .from("lap_traces")
      .select(
        "lap_number, sample_count, max_speed_kmh, min_speed_kmh, max_rpm, min_rpm, avg_rpm, " +
          "max_temp_c, min_temp_c, avg_temp_c, powerzone_pct",
      )
      .eq("session_db_id", sessionId)
      .returns<
        {
          lap_number: number;
          sample_count: number | null;
          max_speed_kmh: number | null;
          min_speed_kmh: number | null;
          max_rpm: number | null;
          min_rpm: number | null;
          avg_rpm: number | null;
          max_temp_c: number | null;
          min_temp_c: number | null;
          avg_temp_c: number | null;
          powerzone_pct: number | null;
        }[]
      >(),
  ]);

  const traceByLap = new Map((traces ?? []).map((t) => [t.lap_number, t]));
  const rows: EngineLapRow[] = (laps ?? []).map((lap) => {
    const t = traceByLap.get(lap.lap_number);
    return {
      lapNumber: lap.lap_number,
      lapTimeS: lap.lap_time_s,
      excluded: Boolean(lap.is_outlier) || Boolean(lap.excluded_by_user),
      sampleCount: t?.sample_count ?? null,
      maxSpeedKmh: t?.max_speed_kmh ?? null,
      minSpeedKmh: t?.min_speed_kmh ?? null,
      maxRpm: t?.max_rpm ?? null,
      minRpm: t?.min_rpm ?? null,
      avgRpm: t?.avg_rpm ?? null,
      maxTempC: t?.max_temp_c ?? null,
      minTempC: t?.min_temp_c ?? null,
      avgTempC: t?.avg_temp_c ?? null,
      powerzonePct: t?.powerzone_pct ?? null,
    };
  });

  return (
    <main className="mx-auto max-w-[1400px] px-6 py-8">
      <AppHeader email={appUser?.email} current="/" />
      <EngineAnalysis
        sessionId={sessionId}
        driverName={session.driver_profiles?.display_name ?? "Session"}
        trackName={session.track_name}
        startDate={session.start_date}
        startTime={session.start_time}
        engineCategory={appUser?.engine_category ?? null}
        laps={rows}
      />
    </main>
  );
}
