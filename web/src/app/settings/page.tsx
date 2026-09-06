import { resolveAppUser } from "@/lib/supabase/server";
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

  return (
    <main className="mx-auto max-w-2xl px-6 py-8">
      <AppHeader email={appUser.email} current="/settings" />
      <h1 className="mb-1 text-lg font-semibold">Settings</h1>
      <p className="mb-8 text-sm text-muted">Your account, and what you race.</p>
      <SettingsForm
        userId={appUser.id}
        displayName={appUser.display_name ?? ""}
        engineCategory={appUser.engine_category ?? ""}
      />
    </main>
  );
}
