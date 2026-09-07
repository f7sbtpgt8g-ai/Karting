import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import TracksClient from "./TracksClient";
import type { TrackSummaryRow } from "@/lib/tracks";

export const dynamic = "force-dynamic";

/**
 * Tracks: every track in scope, with the outright best/average lap at it.
 *
 * "In scope" is exactly what `track_summaries` (0011) returns under RLS --
 * your own sessions, your team's team-or-shared ones, and everyone's
 * publicly shared ones. Unlike Home, that full breadth is the point here:
 * this page exists to answer "how does this track compare", which needs the
 * community's shared data, not just your own.
 */
export default async function TracksPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  // Shaped by hand rather than generated: this project has no
  // `supabase gen types` step, and RPC return rows need a type either way.
  type RawTrackSummary = {
    track_name: string;
    session_count: number;
    best_lap_s: number | null;
    best_lap_driver_name: string | null;
    best_lap_engine_category: string | null;
    average_lap_s: number | null;
    last_driven_date: string | null;
  };

  // Cast rather than `.returns<>()`: without generated database types the
  // client cannot tell a set-returning function from a scalar one, and
  // guesses "single object" for the RPC builder (same workaround
  // `admin/page.tsx` already uses for `admin_user_overview`).
  const { data } = await supabase.rpc("track_summaries", { p_engine_category: null });
  const rawTracks = (data ?? []) as RawTrackSummary[];

  const tracks: TrackSummaryRow[] = rawTracks.map((row) => ({
    trackName: row.track_name,
    sessionCount: row.session_count,
    bestLapS: row.best_lap_s,
    bestLapDriverName: row.best_lap_driver_name,
    bestLapEngineCategory: row.best_lap_engine_category,
    averageLapS: row.average_lap_s,
    lastDrivenDate: row.last_driven_date,
  }));

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <AppHeader email={appUser.email} current="/tracks" isAdmin={appUser?.is_admin} />

      {tracks.length === 0 ? (
        <div className="rounded border border-hairline bg-surface p-8 text-center">
          <h1 className="mb-2 text-lg font-semibold">No tracks yet</h1>
          <p className="text-sm text-muted">
            Once a session has a track name, it will show up here with its best and average lap.
          </p>
        </div>
      ) : (
        <TracksClient tracks={tracks} />
      )}
    </main>
  );
}
