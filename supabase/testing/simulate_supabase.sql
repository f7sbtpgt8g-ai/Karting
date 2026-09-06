-- Reproduce the parts of a real Supabase project that RLS behaviour depends
-- on, so `supabase/migrations/*.sql` can be tested against a plain local
-- Postgres and the result actually means something.
--
-- NOT applied to a real project -- a real Supabase project already provides
-- all of this. This file exists so `tests/test_rls_policies.py` can create an
-- equivalent environment locally. Apply it BEFORE the migrations.
--
-- What matters, and why each piece is here:
--
--   * `auth.uid()` -- every policy is written in terms of it (via
--     `current_app_user_id()`). Defined the way Supabase defines it: read
--     the `sub` claim out of the request-scoped `request.jwt.claims` GUC, so
--     a test can switch identity with `SET LOCAL request.jwt.claims` exactly
--     as PostgREST does per request. A hardcoded stub would test nothing.
--
--   * The `anon` / `authenticated` / `service_role` roles, and the default
--     grants Supabase ships. This is the easiest thing to get wrong when
--     reasoning about RLS instead of testing it: RLS only ever *restricts*
--     what a grant already allows, so a table with RLS disabled but grants
--     present is fully open. Supabase grants ALL on every table in `public`
--     to anon and authenticated out of the box, which is exactly why "we
--     just won't enable RLS on that table" is not a safe default there.

CREATE SCHEMA IF NOT EXISTS auth;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE
AS $$
    SELECT coalesce(
        nullif(current_setting('request.jwt.claim.sub', true), ''),
        (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    )::uuid
$$;

CREATE OR REPLACE FUNCTION auth.role() RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT coalesce(
        nullif(current_setting('request.jwt.claim.role', true), ''),
        (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
    )::text
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;

-- The default-privilege grants a Supabase project ships with. Applied both
-- as a default (for tables the migration is about to create) and directly
-- (for anything already present).
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;
