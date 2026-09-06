import { createServerClient, type CookieOptions } from "@supabase/ssr";
import { cookies } from "next/headers";

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
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
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
 * The signed-in user's row in this schema's own `users` table.
 *
 * Supabase Auth identifies people by UUID; this schema keys everything off
 * an integer `users.id` and bridges the two with `users.external_auth_id`.
 * Every RLS policy resolves the caller through that same mapping (see
 * `current_app_user_id()`), so anything writing a row that references a user
 * needs this id rather than `auth.uid()`.
 *
 * Returns null when signed out, or when the account exists in Supabase Auth
 * but has never been mirrored locally -- which is exactly the state that
 * makes RLS silently return nothing, so callers should treat null as "not
 * usable yet" rather than assuming a session implies a row.
 */
export async function getAppUser() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) return null;

  const { data } = await supabase
    .from("users")
    .select("id, email, display_name, email_verified")
    .eq("external_auth_id", user.id)
    .maybeSingle();

  return data ? { ...data, authId: user.id, email: data.email ?? user.email } : null;
}
