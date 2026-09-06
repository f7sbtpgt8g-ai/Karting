import SignOutButton from "./SignOutButton";
import type { AppUserResolution } from "@/lib/supabase/server";

/**
 * What to say when a signed-in account has no local `users` row.
 *
 * The previous version of this screen asserted a cause -- "this address is
 * probably already registered to another account" -- which was a guess, was
 * often wrong, and sent the reader looking in the wrong place. This says
 * which of the three situations it actually is, and shows the auth id, which
 * is the one piece of information needed to find the account in the
 * database.
 */
export default function AccountNotLinked({
  resolution,
}: {
  resolution: Exclude<AppUserResolution, { status: "ok" }>;
}) {
  if (resolution.status === "signed_out") {
    return (
      <Frame title="Not signed in">
        <p className="text-sm text-muted">Sign in to see your sessions.</p>
      </Frame>
    );
  }

  if (resolution.status === "error") {
    return (
      <Frame title="Could not read your account">
        <p className="mb-3 text-sm text-muted">
          You are signed in, but the query for your driver record failed. This is usually a
          migration that has not been applied to the database yet, rather than anything wrong with
          your account.
        </p>
        <pre className="mb-4 overflow-x-auto rounded border border-hairline bg-canvas p-3 text-xs text-loss">
          {resolution.message}
        </pre>
        <Identity authId={resolution.authId} />
      </Frame>
    );
  }

  return (
    <Frame title="Account not linked yet">
      <p className="mb-3 text-sm text-muted">
        You are signed in as{" "}
        <span className="font-mono text-ink2">{resolution.email ?? "this account"}</span>, but it
        has no driver record in the telemetry database, so it is invisible to every permission
        rule &mdash; signed in, and able to see nothing.
      </p>
      <p className="mb-4 text-sm text-muted">
        Accounts created before the signup link existed are in exactly this state, and are repaired
        by running{" "}
        <code className="text-ink2">supabase/manual/0004_backfill_external_auth_id.sql</code>. If
        this is a brand-new signup, the address may already belong to a different account.
      </p>
      <Identity authId={resolution.authId} />
    </Frame>
  );
}

function Identity({ authId }: { authId: string }) {
  return (
    <div className="rounded border border-hairline bg-canvas p-3">
      <div className="label mb-1">Supabase auth id</div>
      <code className="block break-all font-mono text-xs text-ink2">{authId}</code>
      <p className="mt-2 text-xs text-muted">
        Look it up with{" "}
        <code>select * from users where external_auth_id = &apos;{authId}&apos;</code>.
      </p>
    </div>
  );
}

function Frame({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-2xl px-6 py-16">
      <h1 className="mb-3 text-lg font-semibold">{title}</h1>
      {children}
      <div className="mt-6">
        <SignOutButton />
      </div>
    </main>
  );
}
