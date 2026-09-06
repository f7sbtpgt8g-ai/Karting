import Link from "next/link";
import { createClient, resolveAppUser } from "@/lib/supabase/server";
import AccountNotLinked from "@/components/AccountNotLinked";
import AppHeader from "@/components/AppHeader";
import AdminUsers, { type AdminUserRow } from "./AdminUsers";

export const dynamic = "force-dynamic";

/**
 * Administration.
 *
 * There is no separate admin login: admins sign in like everyone else and
 * carry `users.is_admin`. A second credential system would be a second auth
 * surface to secure -- its own password reset, its own sessions, its own
 * ways to get it wrong -- guarding strictly more than the first one guards.
 *
 * The check below is a courtesy, not the boundary. Both `admin_user_overview`
 * and `admin_delete_user` verify admin status inside the database, so a
 * non-admin who reached this route by any means still gets nothing.
 */
export default async function AdminPage() {
  const resolution = await resolveAppUser();
  if (resolution.status !== "ok") return <AccountNotLinked resolution={resolution} />;
  const appUser = resolution.user;

  const supabase = await createClient();
  // Cast rather than `.returns<>()`: without generated database types the
  // client cannot tell a set-returning function from a scalar one, and
  // guesses "single object" for the RPC builder.
  const { data, error } = await supabase.rpc("admin_user_overview");
  const users = (data ?? []) as AdminUserRow[];

  if (error) {
    // "Not authorised" is the expected answer for everyone who is not an
    // admin, and saying so plainly beats a blank page.
    const forbidden = /not authorised/i.test(error.message);
    return (
      <main className="mx-auto max-w-2xl px-6 py-16">
        <AppHeader email={appUser.email} current="/admin" isAdmin={appUser?.is_admin} />
        <h1 className="mb-3 text-lg font-semibold">
          {forbidden ? "Admins only" : "Could not load the admin view"}
        </h1>
        <p className="text-sm text-muted">
          {forbidden ? (
            <>
              This account is not an administrator. Admin rights are granted with SQL by someone
              with database access &mdash; deliberately not from inside the app, so no flaw in it
              can mint an admin.
            </>
          ) : (
            error.message
          )}
        </p>
        <Link href="/" className="mt-6 inline-block text-sm text-muted underline">
          Back to Home
        </Link>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-[1400px] px-6 py-8">
      <AppHeader email={appUser.email} current="/admin" isAdmin={appUser?.is_admin} />
      <h1 className="mb-1 text-lg font-semibold">Users</h1>
      <p className="mb-6 text-sm text-muted">
        Every account on this installation. {users.length} total.
      </p>
      <AdminUsers users={users} currentUserId={appUser.id} />
    </main>
  );
}
