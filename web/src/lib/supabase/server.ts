import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { cookies } from "next/headers";
import { supabaseAnonKey, supabaseUrl } from "./env";

/**
 * Supabase client for server components and route handlers, carrying the
 * caller's own session cookie.
 *
 * Deliberately the *user's* client, not a service-role one: every query it
 * makes runs as `authenticated` under Row Level Security, which is what the
 * policies in supabase/migrations/0002_rls_hardening.sql were written and
 * tested for. There is no service-role key anywhere in this app -- that key
 * lives only in the background worker's environment, because anything
 * holding it bypasses RLS entirely.
 */
export async function createClient() {
  const cookieStore = await cookies();

  return createServerClient(
    supabaseUrl(),
    supabaseAnonKey(),
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        // Annotated because `cookies` is a union of the current and the
        // deprecated method shapes, which defeats contextual inference.
        setAll(cookiesToSet: { name: string; value: string; options: CookieOptions }[]) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Called from a Server Component, where cookies are read-only.
            // Harmless: middleware refreshes the session on every request,
            // so the write that matters has already happened there.
          }
        },
      },
    },
  );
}

/**
 * Why the signed-in account could not be resolved to a local `users` row.
 *
 * These are three genuinely different situations and used to collapse into
 * one null: not signed in, the query itself failed, and signed in with no
 * mirrored row. The screen that reported them guessed a cause ("this address
 * is probably registered to another account"), which was sometimes simply
 * untrue and sent the reader looking in the wrong place. Better to say which
 * of the three it is, and show the identity involved so it can be looked up.
 */
export type AppUserResolution =
  | { status: "ok"; user: AppUser }
  | { status: "signed_out" }
  | { status: "unlinked"; authId: string; email: string | null }
  | { status: "error"; authId: string; message: string };

export type AppUser = {
  id: number;
  email: string | null;
  display_name: string | null;
  email_verified: boolean | null;
  engine_category: string | null;
  is_admin: boolean;
  authId: string;
};

/**
 * The signed-in user's row in this schema's own `users` table.
 *
 * Supabase Auth identifies people by UUID; this schema keys everything off
 * an integer `users.id` and bridges the two with `users.external_auth_id`.
 * Every RLS policy resolves the caller through that same mapping (see
 * `current_app_user_id()`), so anything writing a row that references a user
 * needs this id rather than `auth.uid()`.
 *
 * An account with no mirrored row authenticates perfectly and is invisible to
 * every policy -- signed in, sees nothing. The two ways to get there are an
 * account created before the signup trigger existed (0004), whose
 * `external_auth_id` is still NULL, and a signup the trigger refused.
 */
export async function resolveAppUser(): Promise<AppUserResolution> {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return { status: "signed_out" };

  const { data, error } = await supabase
    .from("users")
    .select("id, email, display_name, email_verified, engine_category, is_admin")
    .eq("external_auth_id", user.id)
    .maybeSingle();

  // A failed query is not the same as an absent row, and the difference
  // matters: a missing migration and an unlinked account need completely
  // different fixes.
  if (error) {
    return { status: "error", authId: user.id, message: error.message };
  }
  if (!data) {
    return { status: "unlinked", authId: user.id, email: user.email ?? null };
  }
  return {
    status: "ok",
    user: { ...data, authId: user.id, email: data.email ?? user.email ?? null } as AppUser,
  };
}

/** The resolved user, or null for any of the reasons above. */
export async function getAppUser(): Promise<AppUser | null> {
  const resolution = await resolveAppUser();
  return resolution.status === "ok" ? resolution.user : null;
}
