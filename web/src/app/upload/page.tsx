import Link from "next/link";
import { createClient, getAppUser } from "@/lib/supabase/server";
import AppHeader from "@/components/AppHeader";
import SignOutButton from "@/components/SignOutButton";
import UploadForm from "./UploadForm";

export const dynamic = "force-dynamic";

const STATUS_STYLE: Record<string, string> = {
  pending: "text-muted",
  processing: "text-theoretical",
  complete: "text-gain",
  failed: "text-loss",
};

export default async function UploadPage() {
  const appUser = await getAppUser();

  // Middleware guarantees a Supabase session by the time we get here, but not
  // a mirrored `users` row -- and without one, every RLS policy resolves the
  // caller to NULL, so an upload would insert a batch owned by nobody.
  //
  // The signup trigger (0004) creates that row inside the signup, so reaching
  // this branch means something is genuinely wrong -- the migration hasn't
  // been applied, or this address is already claimed by a different identity.
  // Either way, say so rather than presenting a form that cannot work.
  if (!appUser) {
    return (
      <main className="mx-auto max-w-2xl px-6 py-16">
        <h1 className="mb-3 text-lg font-semibold">Account not linked yet</h1>
        <p className="text-sm text-muted">
          You are signed in, but this account has no driver record in the telemetry database, so
          nothing you upload could be filed against you. This usually means the address is already
          registered to a different account. Sign out and sign in with that one, or get in touch.
        </p>
        <div className="mt-6">
          <SignOutButton />
        </div>
      </main>
    );
  }

  const supabase = await createClient();

  const [{ data: profiles }, { data: batches }] = await Promise.all([
    supabase
      .from("driver_profiles")
      .select("id, display_name")
      .eq("user_id", appUser.id)
      .order("display_name"),
    supabase
      .from("upload_batches")
      .select("id, original_filename, status, error_message, sessions_created, created_at")
      .order("created_at", { ascending: false })
      .limit(10),
  ]);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <AppHeader email={appUser.email} current="/upload" />

      <h2 className="mb-2 text-lg font-semibold">Upload a session</h2>
      <p className="mb-8 text-sm text-muted">
        Export from Unipro Analyser as a tab-separated file. One export can hold a whole track
        day &mdash; every session inside it is stored separately.
      </p>

      <UploadForm profiles={profiles ?? []} />

      <section className="mt-14">
        <h2 className="label mb-3">Recent uploads</h2>
        {!batches || batches.length === 0 ? (
          <p className="text-sm text-muted">Nothing uploaded yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-hairline text-left">
                <th className="label py-2 font-normal">File</th>
                <th className="label py-2 font-normal">When</th>
                <th className="label py-2 font-normal">Sessions</th>
                <th className="label py-2 font-normal">Status</th>
              </tr>
            </thead>
            <tbody>
              {batches.map((batch) => (
                <tr key={batch.id} className="border-b border-hairline/60 align-top">
                  <td className="py-2 pr-4 font-mono text-xs">
                    {batch.original_filename ?? "(unnamed)"}
                  </td>
                  <td className="py-2 pr-4 text-muted">
                    {new Date(batch.created_at).toLocaleString()}
                  </td>
                  <td className="py-2 pr-4 tabular-nums">{batch.sessions_created ?? "—"}</td>
                  <td className={`py-2 ${STATUS_STYLE[batch.status] ?? "text-muted"}`}>
                    {batch.status}
                    {batch.error_message && (
                      <span className="block max-w-md text-xs text-muted">
                        {batch.error_message}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <p className="mt-10 text-xs text-muted">
        Analysis still lives in the{" "}
        <Link href="https://karting.streamlit.app" className="underline">
          Streamlit app
        </Link>{" "}
        while the migration is in progress. Sessions uploaded here appear there.
      </p>
    </main>
  );
}
