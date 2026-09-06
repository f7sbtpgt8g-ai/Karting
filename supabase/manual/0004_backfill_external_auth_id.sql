-- NOT a migration. Run this by hand, once, after reviewing what it would do.
--
-- It lives outside supabase/migrations/ deliberately: everything in there is
-- applied automatically by `supabase db push` / the GitHub integration, and
-- this is the one piece of the auth-mirroring work that **writes to rows that
-- already exist** in the live Streamlit app's `users` table. That is exactly
-- the class of change worth looking at before it runs, so it is opt-in.
--
-- What it fixes
-- -------------
-- Accounts that registered through Streamlit against Supabase Auth before
-- 0004's trigger existed have `users.external_auth_id = NULL`. They sign in
-- perfectly, and every RLS policy -- which resolves the caller via
-- `current_app_user_id()` -> `external_auth_id` -- sees NULL and denies them
-- everything. In the browser that looks like "logged in, no data, no error".
--
-- These accounts also repair themselves the next time they sign in through
-- Streamlit (`telemetry/auth.py`'s `_mirror_user` backfills the column), so
-- this script is a convenience, not a prerequisite. It just means nobody has
-- to sign into the old app first.
--
-- Safety
-- ------
--   * Only ever fills a column that is currently NULL -- it cannot re-point
--     an already-linked account at a different Supabase identity.
--   * Matches on email only, and skips any auth identity already claimed by
--     another local account.
--   * Touches nothing but `users.external_auth_id`.
--
-- Look before you leap: run the SELECT first, check the rows are the pairings
-- you expect, then run the UPDATE.

-- 1. What would be linked, and to whom.
SELECT u.id AS local_user_id, u.email, u.display_name, a.id AS supabase_auth_id
  FROM users u
  JOIN auth.users a ON lower(a.email) = u.email
 WHERE u.external_auth_id IS NULL
   AND a.email IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM users other WHERE other.external_auth_id = a.id::text)
 ORDER BY u.id;

-- 2. Apply it.
-- UPDATE users u
--    SET external_auth_id = a.id::text
--   FROM auth.users a
--  WHERE u.external_auth_id IS NULL
--    AND a.email IS NOT NULL
--    AND lower(a.email) = u.email
--    AND NOT EXISTS (SELECT 1 FROM users other WHERE other.external_auth_id = a.id::text);
