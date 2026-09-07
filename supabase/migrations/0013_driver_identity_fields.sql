-- Driver identity: first name, last name, country, and a real self-service
-- fix for "preferred name" -- the field every leaderboard, roster and
-- session-attribution dropdown already shows and has never been editable.
--
-- first_name/last_name/country are new, private account data on `users` --
-- the same category date_of_birth/guardian_email already are on this
-- table, and just as nullable: "required" is a client-side form rule
-- (LoginForm.tsx, SettingsForm.tsx), not a DB constraint, because a hard
-- failure in handle_new_auth_user() below would abort Supabase Auth account
-- creation entirely for every signup path that doesn't carry this metadata
-- (OAuth, magic links, an admin-created account), not just this one form.
--
-- "Preferred name" is NOT a new column. It reuses driver_profiles.
-- display_name, which was already, semantically, exactly "what this driver
-- is called publicly" -- every leaderboard, the Teams roster, Home's
-- driver grouping and the Upload attribution dropdown already read it.
-- What was missing was a way for its owner to write it: driver_profiles
-- has never had an UPDATE policy, so SettingsForm.tsx has silently been
-- writing users.display_name instead -- a column nothing public ever
-- reads. This migration fixes that at the root rather than adding a
-- fourth column with the same meaning.

-- ---------------------------------------------------------------------------
-- 1. New columns on `users`.
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT;

-- A driver may set these three, and nothing else on their user row -- same
-- reasoning as 0007's engine_category/display_name grant. Additive: 0007's
-- REVOKE UPDATE ON users already stripped the default GRANT ALL, so this
-- only needs to hand back the new columns, not restate the old ones (no
-- later migration in this history has ever restated a prior users GRANT).
GRANT UPDATE (first_name, last_name, country) ON users TO authenticated;

-- ---------------------------------------------------------------------------
-- 2. handle_new_auth_user(): redefined rather than patched (CREATE OR
--    REPLACE FUNCTION needs the whole body; the trigger itself, and the
--    DO-block wrapper guarding against a Postgres without an `auth` schema,
--    are unchanged from 0007). Adds first_name/last_name/country metadata,
--    and changes v_display's fallback from "email" to "last name, then
--    email" -- the actual "default to Lastname" behaviour. The guardian-
--    consent computation block is untouched, byte for byte.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = 'auth' AND table_name = 'users'
) THEN

EXECUTE $mirror$

CREATE OR REPLACE FUNCTION public.handle_new_auth_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_user_id     BIGINT;
    v_linked      TEXT;
    v_display     TEXT;
    v_dob         TEXT;
    v_dob_date    DATE;
    v_guardian    TEXT;
    v_engine      TEXT;
    v_first       TEXT;
    v_last        TEXT;
    v_country     TEXT;
    v_consent     TEXT := 'not_required';
BEGIN
    IF NEW.email IS NULL THEN
        RETURN NEW;
    END IF;

    v_display  := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'display_name', '')), '');
    v_dob      := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'date_of_birth', '')), '');
    v_guardian := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'guardian_email', '')), '');
    v_engine   := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'engine_category', '')), '');
    v_first    := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'first_name', '')), '');
    v_last     := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'last_name', '')), '');
    v_country  := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'country', '')), '');
    -- "Default to Lastname": the preferred-name field falls back to the
    -- driver's last name when left blank, and only as a last resort to the
    -- account email -- for a signup path that carries neither (OAuth, magic
    -- links, an admin-created account). v_display feeds
    -- driver_profiles.display_name, which is NOT NULL, so this must never
    -- end up NULL.
    v_display  := COALESCE(v_display, v_last, lower(NEW.email));

    IF v_dob IS NOT NULL THEN
        BEGIN
            v_dob_date := v_dob::date;
        EXCEPTION WHEN others THEN
            v_dob_date := NULL;
            v_dob := NULL;
        END;
        IF v_dob_date IS NOT NULL AND v_dob_date > (current_date - interval '16 years') THEN
            v_consent := 'pending';
        END IF;
    END IF;

    SELECT id, external_auth_id INTO v_user_id, v_linked
      FROM users WHERE email = lower(NEW.email);

    IF v_user_id IS NOT NULL THEN
        IF v_linked IS NOT NULL AND v_linked <> NEW.id::text THEN
            RAISE EXCEPTION 'That email address is already linked to a different account.';
        END IF;
        UPDATE users
           SET external_auth_id = NEW.id::text,
               -- Only fill a value in, never overwrite one the driver has
               -- already set: this branch also runs for a Streamlit-era
               -- account crossing over.
               engine_category = COALESCE(engine_category, v_engine),
               first_name = COALESCE(first_name, v_first),
               last_name = COALESCE(last_name, v_last),
               country = COALESCE(country, v_country)
         WHERE id = v_user_id AND external_auth_id IS NULL;
    ELSE
        INSERT INTO users (
            email, external_auth_id, email_verified, display_name,
            date_of_birth, guardian_email, guardian_consent_status,
            engine_category, first_name, last_name, country, created_at
        ) VALUES (
            lower(NEW.email), NEW.id::text, NEW.email_confirmed_at IS NOT NULL, v_display,
            v_dob, v_guardian, v_consent, v_engine, v_first, v_last, v_country, now()
        )
        RETURNING id INTO v_user_id;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM driver_profiles WHERE user_id = v_user_id) THEN
        INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at)
        VALUES (v_display, v_user_id, 'claimed', now(), now());
    END IF;

    RETURN NEW;
END;
$fn$;

$mirror$;

END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. driver_profiles: a driver may finally edit their own preferred name.
--
--    RLS decides which *rows* an UPDATE may touch, never which columns --
--    this table has never had a GRANT statement of its own, so it still
--    carries Supabase's default GRANT ALL to anon/authenticated. Without
--    the REVOKE below, this new policy alone would let a driver rewrite
--    claim_status, user_id, or claim_token on their own row -- the same
--    "policy narrows rows, grant narrows columns" gap 0006/0007/0010
--    already closed for laps/users/team_memberships, closed here the same
--    way.
--
--    USING/WITH CHECK mirrors driver_profiles_select's own ownership
--    predicate (0001): user_id = current_app_user_id().
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS driver_profiles_update_own ON driver_profiles;
CREATE POLICY driver_profiles_update_own ON driver_profiles
    FOR UPDATE
    USING (user_id = current_app_user_id())
    WITH CHECK (user_id = current_app_user_id());

REVOKE UPDATE ON driver_profiles FROM anon, authenticated;
GRANT UPDATE (display_name) ON driver_profiles TO authenticated;
