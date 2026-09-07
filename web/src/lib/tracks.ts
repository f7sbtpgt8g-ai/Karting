/**
 * Row shapes returned by the `track_*` RPCs (`supabase/migrations/
 * 0011_track_leaderboards.sql`), and podium-place styling.
 *
 * Kept apart from the Tracks page components the same way `teams.ts` is:
 * pure, testable, and shared between the list page's "best lap" cell and the
 * detail page's two podiums.
 */

export type TrackSummaryRow = {
  trackName: string;
  sessionCount: number;
  bestLapS: number | null;
  bestLapDriverName: string | null;
  bestLapDriverCountry: string | null;
  bestLapEngineCategory: string | null;
  averageLapS: number | null;
  lastDrivenDate: string | null; // ISO date from the RPC's DATE column
};

export type MyBestRow = {
  bestLapS: number | null;
  averageLapS: number | null;
  sessionCount: number;
  lastDrivenDate: string | null;
};

export type DriverPodiumRow = {
  rank: number;
  driverProfileId: number;
  driverName: string;
  driverCountry: string | null;
  bestLapS: number;
  engineCategory: string | null;
  teamName: string | null;
};

export type TeamPodiumRow = {
  rank: number;
  teamId: number;
  teamName: string;
  bestLapS: number;
  fastestDriverName: string;
  fastestDriverCountry: string | null;
};

/**
 * Place-based accent for the top three rows of a podium list. Ad-hoc rather
 * than a new Tailwind token: this app's palette (accent/gain/loss/reference/
 * theoretical) already carries specific meanings elsewhere, and none of them
 * mean "1st/2nd/3rd" -- reusing one would make a podium argue with whatever
 * else that colour means on the same page (theoretical's yellow especially,
 * since it already means "theoretical best" one click away on the session
 * detail page). Also kept distinct from `ENGINE_COLORS`/`CONDITION_COLOR` --
 * `tracks.test.ts` asserts that, the same way `conditions.test.ts` already
 * does for that pair.
 */
export const PODIUM_PLACE_COLOR: Record<1 | 2 | 3, string> = {
  1: "#e8b64a", // gold
  2: "#c7ccd1", // silver
  3: "#cd8a4e", // bronze
};

export function podiumPlaceColor(rank: number): string | undefined {
  return PODIUM_PLACE_COLOR[rank as 1 | 2 | 3];
}
