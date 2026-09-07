import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import { DriverName } from "@/components/CountryFlag";
import { deriveDisplayedRating } from "@/lib/rating";

export const dynamic = "force-dynamic";

/**
 * The Driver Rating leaderboard -- ranked by the displayed conservative
 * number (mu - k*sigma), same "displayed rating, not raw mu" rule the card
 * on Home follows.
 *
 * `driver_ratings` is open-read to any authenticated driver (0015), so this
 * is a plain embedded select rather than a bespoke RPC -- the `!inner` hint
 * turns the `driver_profiles` embed into the filter this needs (claimed,
 * linked to a real account), the same public-eligibility predicate
 * `track_driver_podium` (0011) applies, reused here for the same reason:
 * a rating built entirely from public-eligible sessions (see
 * telemetry/rating/engine.py's `_PUBLIC_ELIGIBLE_SQL`) should only ever be
 * shown next to other public-eligible drivers, not a stray unclaimed
 * profile.
 *
 * Sorted here in the server component rather than by the query: the
 * displayed rating depends on "now" (sigma keeps decaying since a driver's
 * last verified session -- see lib/rating.ts), so it can't be an `ORDER BY`
 * PostgREST does at the database layer, and there is no interactive state
 * on this page that would otherwise justify a client component.
 */
export default async function RatingPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  type RawRow = {
    driver_profile_id: number;
    mu: number;
    sigma_at_last_update: number;
    last_verified_session_at: string | null;
    sessions_rated_count: number;
    driver_profiles: { display_name: string; country: string | null } | null;
  };

  const { data } = await supabase
    .from("driver_ratings")
    .select(
      "driver_profile_id, mu, sigma_at_last_update, last_verified_session_at, sessions_rated_count, driver_profiles!inner(display_name, country, claim_status, user_id)",
    )
    .eq("driver_profiles.claim_status", "claimed")
    .not("driver_profiles.user_id", "is", null)
    .returns<RawRow[]>();

  const leaderboard = (data ?? [])
    .map((row) => {
      const derived = deriveDisplayedRating({
        mu: row.mu,
        sigmaAtLastUpdate: row.sigma_at_last_update,
        lastVerifiedSessionAt: row.last_verified_session_at,
      });
      return {
        driverProfileId: row.driver_profile_id,
        driverName: row.driver_profiles?.display_name ?? "Unknown driver",
        driverCountry: row.driver_profiles?.country ?? null,
        sessionsRatedCount: row.sessions_rated_count,
        ...derived,
      };
    })
    .sort((a, b) => b.displayedRating - a.displayedRating);

  return (
    <main className="mx-auto max-w-4xl px-6 py-8">
      <AppHeader email={appUser.email} current="/rating" isAdmin={appUser?.is_admin} />
      <h1 className="mb-1 text-lg font-semibold">Driver Rating</h1>
      <p className="mb-6 text-sm text-muted">
        Field-relative pace, not raw lap times -- see each driver&apos;s own rating card on Home for why theirs
        moved.
      </p>

      {leaderboard.length === 0 ? (
        <p className="text-sm text-muted">No rated drivers yet.</p>
      ) : (
        <div className="divide-y divide-hairline rounded border border-hairline bg-surface">
          {leaderboard.map((row, i) => (
            <div key={row.driverProfileId} className="flex items-center gap-3 px-4 py-2.5 text-sm">
              <span className="w-6 text-right font-mono font-bold text-muted">{i + 1}</span>
              <span className="flex-1 font-semibold">
                <DriverName name={row.driverName} country={row.driverCountry} />
              </span>
              {row.provisional && (
                <span className="rounded bg-theoretical/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-theoretical">
                  Provisional
                </span>
              )}
              <span className="text-xs text-muted">{row.sessionsRatedCount} rated</span>
              <span className="font-mono text-sm font-bold">{Math.round(row.displayedRating)}</span>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
