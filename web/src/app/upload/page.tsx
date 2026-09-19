import Link from "next/link";
import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import UploadForm from "./UploadForm";
import UnassignedSessions, { type UnassignedSessionRow } from "./UnassignedSessions";

export const dynamic = "force-dynamic";

const STATUS_STYLE: Record<string, string> = {
  pending: "text-muted",
  processing: "text-theoretical",
  complete: "text-gain",
  failed: "text-loss",
};

export default async function UploadPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();

  const [{ data: profiles }, { data: batches }, { data: unassignedRows }, { data: assignableProfiles }] =
    await Promise.all([
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
      // Sessions from a "Decide after parsing" upload -- driver_profile_id
      // is NULL, which `sessions_select` (0002) can only satisfy via its
      // `uploaded_by_user_id = current_app_user_id()` branch, so this comes
      // back scoped to the caller's own uploads without an explicit filter.
      // Without a surface to find these again, a driver picking "decide
      // after parsing" has no way back to them -- they're saved, but they
      // never appear on Home (`driver_profile_id IN (...)` never matches
      // NULL), and nothing else lists them either.
      supabase
        .from("sessions")
        .select("id, track_name, session_type, start_date, start_time, n_laps, best_lap_s")
        .is("driver_profile_id", null)
        .eq("uploaded_by_user_id", appUser.id)
        .order("start_date", { ascending: false })
        .returns<UnassignedSessionRow[]>(),
      // Every driver this account is allowed to assign a session to:
      // `driver_profiles_select` (0001) already resolves this to every
      // *claimed* profile on the platform (a teammate included) plus this
      // account's own profile and anything it created (e.g. an unclaimed
      // profile added from the sync tool) -- no extra filter needed here,
      // RLS is doing the actual narrowing.
      supabase.from("driver_profiles").select("id, display_name").order("display_name"),
    ]);

  return (
    <main className="mx-auto max-w-3xl px-6 py-12">
      <AppHeader email={appUser.email} current="/upload" isAdmin={appUser?.is_admin} />

      <h2 className="mb-2 text-lg font-semibold">Upload a session</h2>
      <p className="mb-8 text-sm text-muted">
        Export from Unipro Analyser as a tab-separated file. One export can hold a whole track
        day &mdash; every session inside it is stored separately.
      </p>

      <UnassignedSessions
        sessions={unassignedRows ?? []}
        profiles={assignableProfiles ?? []}
      />

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
        Parsing runs in the background and takes a couple of minutes for a full track day. Once an
        upload reads <span className="text-gain">complete</span>, its sessions are on{" "}
        <Link href="/" className="underline">
          Home
        </Link>
        .
      </p>
    </main>
  );
}
