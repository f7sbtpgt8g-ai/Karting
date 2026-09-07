"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { ENGINE_CATEGORIES, POWERZONE_RPM, hasPowerzone } from "@/lib/engine";
import { COUNTRIES } from "@/lib/countries";

export default function SettingsForm({
  userId,
  driverProfileId,
  preferredName: initialPreferredName,
  firstName: initialFirstName,
  lastName: initialLastName,
  country: initialCountry,
  engineCategory: initialCategory,
}: {
  userId: number;
  driverProfileId: number | null;
  preferredName: string;
  firstName: string;
  lastName: string;
  country: string;
  engineCategory: string;
}) {
  const router = useRouter();
  const [preferredName, setPreferredName] = useState(initialPreferredName);
  const [firstName, setFirstName] = useState(initialFirstName);
  const [lastName, setLastName] = useState(initialLastName);
  const [country, setCountry] = useState(initialCountry);
  const [engineCategory, setEngineCategory] = useState(initialCategory);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);

    // Empty preferred-name input falls back to last name at save time, same
    // as the signup form -- not live-mirrored while typing.
    const finalPreferredName = preferredName.trim() || lastName.trim();

    // `users_update_own` plus a column-level GRANT (0007, extended 0013)
    // allow exactly these five columns and no others -- email, the auth
    // link and the guardian consent state sit in the same row.
    const { error: usersError } = await createClient()
      .from("users")
      .update({
        first_name: firstName.trim() || null,
        last_name: lastName.trim() || null,
        country: country || null,
        engine_category: engineCategory || null,
        display_name: finalPreferredName || null,
      })
      .eq("id", userId);

    // driver_profiles.display_name is the column every leaderboard, the
    // Teams roster and Home's driver grouping actually read -- this is the
    // write that makes "preferred name" real (0013's driver_profiles_
    // update_own policy + column grant). Skipped only for the edge case of
    // an account with no driver_profiles row yet.
    const { error: profileError } = driverProfileId
      ? await createClient()
          .from("driver_profiles")
          .update({ display_name: finalPreferredName })
          .eq("id", driverProfileId)
      : { error: null };

    setBusy(false);
    const firstFailure = usersError ?? profileError;
    if (firstFailure) {
      setError(firstFailure.message);
      return;
    }
    setSaved(true);
    router.refresh();
  }

  return (
    <form onSubmit={save} className="space-y-6">
      <div>
        <label className="label mb-1 block" htmlFor="firstName">
          First name
        </label>
        <input
          id="firstName"
          required
          value={firstName}
          onChange={(e) => setFirstName(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
      </div>

      <div>
        <label className="label mb-1 block" htmlFor="lastName">
          Last name
        </label>
        <input
          id="lastName"
          required
          value={lastName}
          onChange={(e) => setLastName(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
      </div>

      <div>
        <label className="label mb-1 block" htmlFor="country">
          Country
        </label>
        <select
          id="country"
          required
          value={country}
          onChange={(e) => setCountry(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        >
          <option value="" disabled>
            Select a country
          </option>
          {COUNTRIES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="label mb-1 block" htmlFor="name">
          Preferred name
        </label>
        <input
          id="name"
          value={preferredName}
          onChange={(e) => setPreferredName(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <p className="mt-1 text-xs text-muted">
          How you appear to other drivers and on leaderboards. Leave blank to use your last name.
        </p>
      </div>

      <div>
        <label className="label mb-1 block" htmlFor="engine">
          Engine class
        </label>
        <select
          id="engine"
          value={engineCategory}
          onChange={(e) => setEngineCategory(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        >
          <option value="">Not set</option>
          {ENGINE_CATEGORIES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-muted">
          {hasPowerzone(engineCategory)
            ? `Engine analysis shows time in the ${POWERZONE_RPM[0].toLocaleString()}–${POWERZONE_RPM[1].toLocaleString()} rpm power band.`
            : "Powerzone % is a Rotax figure, so it is hidden for this class."}{" "}
          Changing this re-reads every session you have, past and future.
        </p>
      </div>

      <div className="flex items-center gap-4">
        <button
          type="submit"
          disabled={busy || !firstName.trim() || !lastName.trim() || !country}
          className="rounded bg-accent px-5 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {busy ? "Saving..." : "Save"}
        </button>
        {saved && <span className="text-sm text-gain">Saved.</span>}
        {error && (
          <span className="text-sm text-loss" role="alert">
            {error}
          </span>
        )}
      </div>
    </form>
  );
}
