import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import SettingsForm from "./SettingsForm";

export const dynamic = "force-dynamic";

/**
 * Account settings.
 *
 * The engine class lives here rather than anywhere quicker to reach because
 * it changes about once a season -- putting it behind a click is right, and
 * putting it next to the per-session dropdowns would invite changing it by
 * accident, which silently re-reads every past session's engine analysis
 * against a different RPM band.
 */
export default async function SettingsPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  const { data: identity } = await supabase
    .from("users")
    .select("first_name, last_name, country")
    .eq("id", appUser.id)
    .maybeSingle();

  // The caller's own driver_profiles row -- the same lookup Home
  // (web/src/app/page.tsx) already does, needed here for the preferred-name
  // write's WHERE clause and its current value. driver_profiles.display_name,
  // not users.display_name, is what every leaderboard/roster actually shows.
  const { data: myProfile } = await supabase
    .from("driver_profiles")
    .select("id, display_name")
    .eq("user_id", appUser.id)
    .maybeSingle();

  return (
    <main className="mx-auto max-w-2xl px-6 py-8">
      <AppHeader email={appUser.email} current="/settings" isAdmin={appUser?.is_admin} />
      <h1 className="mb-1 text-lg font-semibold">Settings</h1>
      <p className="mb-8 text-sm text-muted">Your account, and what you race.</p>
      <SettingsForm
        userId={appUser.id}
        driverProfileId={myProfile?.id ?? null}
        preferredName={myProfile?.display_name ?? ""}
        firstName={identity?.first_name ?? ""}
        lastName={identity?.last_name ?? ""}
        country={identity?.country ?? ""}
        engineCategory={appUser.engine_category ?? ""}
      />
    </main>
  );
}
