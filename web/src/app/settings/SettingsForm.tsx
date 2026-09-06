"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { ENGINE_CATEGORIES, POWERZONE_RPM, hasPowerzone } from "@/lib/engine";

export default function SettingsForm({
  userId,
  displayName: initialName,
  engineCategory: initialCategory,
}: {
  userId: number;
  displayName: string;
  engineCategory: string;
}) {
  const router = useRouter();
  const [displayName, setDisplayName] = useState(initialName);
  const [engineCategory, setEngineCategory] = useState(initialCategory);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);

    // `users_update_own` plus a column-level GRANT (0007) allow exactly these
    // two columns and no others -- email, the auth link and the guardian
    // consent state sit in the same row.
    const { error: updateError } = await createClient()
      .from("users")
      .update({
        display_name: displayName.trim() || null,
        engine_category: engineCategory || null,
      })
      .eq("id", userId);

    setBusy(false);
    if (updateError) {
      setError(updateError.message);
      return;
    }
    setSaved(true);
    router.refresh();
  }

  return (
    <form onSubmit={save} className="space-y-6">
      <div>
        <label className="label mb-1 block" htmlFor="name">
          Driver name
        </label>
        <input
          id="name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          className="w-full rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <p className="mt-1 text-xs text-muted">How you appear to other drivers and on leaderboards.</p>
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
          disabled={busy}
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
