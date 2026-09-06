/**
 * Reading the two Supabase environment variables, with the one misconfiguration
 * that is genuinely hard to diagnose caught up front.
 *
 * The Supabase dashboard shows several URLs, and the one labelled as the Data
 * API / RESTful endpoint ends in `/rest/v1`. Paste that instead of the project
 * URL and supabase-js appends its own path to it -- so a sign-up posts to
 * `/rest/v1/auth/v1/signup`, PostgREST answers instead of GoTrue, and the user
 * is shown `Invalid path specified in request URL` (PGRST125). Nothing in that
 * message points at the environment variable, and every other page still looks
 * fine until someone tries to authenticate.
 *
 * So it is worth one explicit check. Better a deployment that says which
 * variable is wrong than one that authenticates against the wrong service.
 */

const SERVICE_PATH = /\/(rest|auth|storage|realtime|functions)\/v\d+\/?$/i;

export function supabaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  if (!raw) {
    throw new Error(
      "NEXT_PUBLIC_SUPABASE_URL is not set. It should be your project URL, " +
        "e.g. https://<project-ref>.supabase.co",
    );
  }

  const url = raw.replace(/\/+$/, "");
  if (SERVICE_PATH.test(url)) {
    throw new Error(
      `NEXT_PUBLIC_SUPABASE_URL should be the project URL only, not a service ` +
        `endpoint -- got "${url}". Drop the trailing path so it reads ` +
        `"${url.replace(SERVICE_PATH, "")}". supabase-js adds /auth/v1, ` +
        `/rest/v1 and /storage/v1 itself.`,
    );
  }
  return url;
}

export function supabaseAnonKey(): string {
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim();
  if (!key) {
    throw new Error(
      "NEXT_PUBLIC_SUPABASE_ANON_KEY is not set. Use the project's publishable " +
        "(anon) key -- never the secret/service-role key, which bypasses RLS " +
        "and would be inlined into the browser bundle.",
    );
  }
  return key;
}
