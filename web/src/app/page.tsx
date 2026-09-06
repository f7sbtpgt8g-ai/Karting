import Link from "next/link";
import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import HomeClient, { type SessionRow } from "./HomeClient";

export const dynamic = "force-dynamic";

/**
 * Home: every session in scope, grouped by driver -- the Streamlit app's
 * landing page (`page_home`).
 *
 * "In scope" is deliberately narrower than what RLS permits. The policies
 * let you read your own sessions, your teammates' team-visible ones, and
 * every publicly shared session in the app; this page shows only the first
 * two, and the second only if you manage the team. Showing every stranger's
 * shared session here would bury your own -- those belong on Leaderboards
 * and Shared Laps instead. RLS is still what makes it safe; this is just
 * what makes it useful.
 */
export default async function HomePage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  const { data: myProfile } = await supabase
    .from("driver_profiles")
    .select("id, display_name")
    .eq("user_id", appUser.id)
    .maybeSingle();

  let scopeIds: number[] = myProfile ? [myProfile.id] : [];
  let elevatedRole: string | null = null;
  let onATeam = false;

  if (myProfile) {
    const { data: membership } = await supabase
      .from("team_memberships")
      .select("team_id, role, status")
      .eq("driver_profile_id", myProfile.id)
      .eq("status", "active")
      .maybeSingle();

    if (membership) {
      onATeam = true;
      // A manager or admin is responsible for comparing their drivers, so
      // their Home covers the whole roster. A plain member's does not --
      // being on a team is not a licence to browse teammates by default.
      if (membership.role === "manager" || membership.role === "admin") {
        elevatedRole = membership.role;
        const { data: roster } = await supabase
          .from("team_memberships")
          .select("driver_profile_id")
          .eq("team_id", membership.team_id)
          .eq("status", "active");
        scopeIds = Array.from(
          new Set([...scopeIds, ...(roster ?? []).map((r) => r.driver_profile_id as number)]),
        );
      }
    }
  }

  // Shaped by hand rather than generated: this project has no
  // `supabase gen types` step yet, and without one the client infers a
  // parse-error type for any select carrying an embed.
  type RawSession = {
    id: number;
    track_name: string | null;
    session_type: string | null;
    start_date: string | null;
    start_time: string | null;
    n_laps: number | null;
    best_lap_s: number | null;
    track_condition: string | null;
    kart_class: string | null;
    engine_category: string | null;
    visibility: string;
    driver_profile_id: number | null;
    driver_profiles: { display_name: string } | null;
  };

  const rows: RawSession[] = scopeIds.length
    ? ((
        await supabase
          .from("sessions")
          .select(
            "id, track_name, session_type, start_date, start_time, n_laps, best_lap_s, " +
              "track_condition, kart_class, engine_category, visibility, driver_profile_id, " +
              "driver_profiles(display_name)",
          )
          .in("driver_profile_id", scopeIds)
          .returns<RawSession[]>()
      ).data ?? [])
    : [];

  const sessions: SessionRow[] = rows.map((row) => ({
    id: row.id,
    trackName: row.track_name,
    sessionType: row.session_type,
    startDate: row.start_date,
    startTime: row.start_time,
    nLaps: row.n_laps,
    bestLapS: row.best_lap_s,
    trackCondition: row.track_condition,
    kartClass: row.kart_class,
    engineCategory: row.engine_category,
    visibility: row.visibility,
    driverProfileId: row.driver_profile_id,
    driverName: row.driver_profiles?.display_name ?? "Unknown driver",
  }));

  return (
    <main className="mx-auto max-w-6xl px-6 py-8">
      <AppHeader email={appUser.email} current="/" isAdmin={appUser?.is_admin} />

      {sessions.length === 0 ? (
        <div className="rounded border border-hairline bg-surface p-8 text-center">
          <h1 className="mb-2 text-lg font-semibold">No sessions yet</h1>
          <p className="mb-6 text-sm text-muted">
            Upload a Unipro export and it will appear here, grouped by driver.
          </p>
          <Link
            href="/upload"
            className="inline-block rounded bg-accent px-5 py-2 text-sm font-semibold text-white"
          >
            Upload a session
          </Link>
        </div>
      ) : (
        <HomeClient
          sessions={sessions}
          myProfileId={myProfile?.id ?? null}
          elevatedRole={elevatedRole}
          onATeam={onATeam}
        />
      )}
    </main>
  );
}
