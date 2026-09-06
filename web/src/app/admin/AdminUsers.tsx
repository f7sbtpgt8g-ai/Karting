"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export type AdminUserRow = {
  id: number;
  email: string | null;
  display_name: string | null;
  engine_category: string | null;
  is_admin: boolean;
  email_verified: boolean | null;
  is_linked: boolean;
  guardian_consent_status: string | null;
  created_at: string | null;
  last_login_at: string | null;
  session_count: number;
  upload_count: number;
  lap_count: number;
  last_session_at: string | null;
};

function when(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function ago(value: string | null): string {
  if (!value) return "never";
  const days = Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000);
  if (Number.isNaN(days)) return "—";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

const COLUMNS = "minmax(14rem,2fr) 7rem 6rem 5rem 5rem 5rem 6rem 8rem";

export default function AdminUsers({
  users,
  currentUserId,
}: {
  users: AdminUserRow[];
  currentUserId: number;
}) {
  const router = useRouter();
  const [confirming, setConfirming] = useState<number | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function remove(user: AdminUserRow) {
    setBusy(true);
    setError(null);
    setDone(null);

    const { data, error: rpcError } = await createClient().rpc("admin_delete_user", {
      target_user_id: user.id,
    });

    setBusy(false);
    if (rpcError) {
      setError(rpcError.message);
      return;
    }
    const summary = data as { sessions_deleted?: number; auth_identity_deleted?: boolean } | null;
    setDone(
      `Deleted ${user.email ?? `user ${user.id}`} — ${summary?.sessions_deleted ?? 0} session(s) removed` +
        (summary?.auth_identity_deleted === false
          ? ". Their sign-in identity could NOT be removed, so they can register again."
          : "."),
    );
    setConfirming(null);
    setTyped("");
    router.refresh();
  }

  return (
    <div>
      {error && (
        <p className="mb-3 rounded border border-loss/40 bg-loss/10 px-3 py-2 text-sm text-loss" role="alert">
          {error}
        </p>
      )}
      {done && (
        <p className="mb-3 rounded border border-gain/40 bg-gain/10 px-3 py-2 text-sm text-gain" role="status">
          {done}
        </p>
      )}

      <div className="overflow-x-auto rounded border border-hairline bg-surface">
        <div className="min-w-[1100px]">
          <div
            className="grid gap-2 border-b border-hairline px-3 py-2"
            style={{ gridTemplateColumns: COLUMNS }}
          >
            <span className="label">Account</span>
            <span className="label">Class</span>
            <span className="label">Joined</span>
            <span className="label text-right">Sessions</span>
            <span className="label text-right">Uploads</span>
            <span className="label text-right">Laps</span>
            <span className="label">Last seen</span>
            <span className="label text-right">Actions</span>
          </div>

          {users.map((user) => (
            <div key={user.id}>
              <div
                className="grid items-center gap-2 border-b border-hairline/60 px-3 py-2 hover:bg-rowalt"
                style={{ gridTemplateColumns: COLUMNS }}
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm">
                    {user.display_name || "(no name)"}
                    {user.is_admin && (
                      <span className="ml-2 rounded bg-accent/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-accent">
                        admin
                      </span>
                    )}
                    {user.id === currentUserId && (
                      <span className="ml-1 text-[10px] text-muted">(you)</span>
                    )}
                  </span>
                  <span className="block truncate font-mono text-[11px] text-muted">
                    {user.email ?? "—"}
                  </span>
                  <span className="mt-0.5 flex gap-2 text-[10px]">
                    {!user.is_linked && (
                      <span className="text-loss" title="No external_auth_id: signs in, sees nothing">
                        unlinked
                      </span>
                    )}
                    {!user.email_verified && <span className="text-theoretical">unverified</span>}
                    {user.guardian_consent_status === "pending" && (
                      <span className="text-theoretical">guardian consent pending</span>
                    )}
                  </span>
                </span>

                <span className="truncate text-xs text-ink2">{user.engine_category ?? "—"}</span>
                <span className="text-xs text-muted">{when(user.created_at)}</span>
                <span className="text-right font-mono text-xs">{user.session_count}</span>
                <span className="text-right font-mono text-xs text-ink2">{user.upload_count}</span>
                <span className="text-right font-mono text-xs text-muted">{user.lap_count}</span>
                <span className="text-xs text-muted" title={user.last_login_at ?? "never"}>
                  {ago(user.last_login_at)}
                </span>

                <span className="text-right">
                  {user.id === currentUserId ? (
                    <span className="text-[11px] text-muted">—</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => {
                        setConfirming(confirming === user.id ? null : user.id);
                        setTyped("");
                        setError(null);
                      }}
                      className="text-xs text-loss underline hover:text-ink"
                    >
                      Delete
                    </button>
                  )}
                </span>
              </div>

              {confirming === user.id && (
                <div className="border-b border-hairline bg-canvas px-4 py-4">
                  <p className="mb-2 text-sm">
                    Permanently delete <strong>{user.email}</strong>?
                  </p>
                  <ul className="mb-3 list-disc space-y-1 pl-5 text-xs text-muted">
                    <li>
                      {user.session_count} session(s), {user.lap_count} lap(s) and all of their
                      analysis are removed. This cannot be undone.
                    </li>
                    <li>Their sign-in identity is removed, so they cannot log in again.</li>
                    <li>
                      Teams they founded and driver profiles they created for other people are
                      kept &mdash; those carry other drivers&apos; data.
                    </li>
                  </ul>
                  {/* Type-to-confirm, because the button sits in a row of
                      identical rows and a mis-click is otherwise a permanent
                      loss of somebody's telemetry. */}
                  <label className="mb-3 block">
                    <span className="label mb-1 block">
                      Type the email address to confirm
                    </span>
                    <input
                      value={typed}
                      onChange={(e) => setTyped(e.target.value)}
                      placeholder={user.email ?? ""}
                      className="w-full max-w-sm rounded border border-hairline bg-surface px-3 py-2 text-sm outline-none focus:border-loss"
                    />
                  </label>
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      disabled={busy || typed.trim().toLowerCase() !== (user.email ?? "").toLowerCase()}
                      onClick={() => remove(user)}
                      className="rounded bg-loss px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                    >
                      {busy ? "Deleting..." : "Delete permanently"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirming(null)}
                      className="text-sm text-muted underline"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <p className="mt-3 text-xs text-muted">
        Admin rights are granted with SQL, never from inside the app:{" "}
        <code className="text-ink2">
          UPDATE users SET is_admin = TRUE WHERE email = &apos;…&apos;;
        </code>{" "}
        Every check here is repeated inside the database, so this page is a convenience rather
        than the boundary.
      </p>
    </div>
  );
}
