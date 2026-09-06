-- Mirror Supabase Auth signups into `users` + `driver_profiles`.
--
-- Everything in this schema is keyed off an integer `users.id`, and every RLS
-- policy resolves the caller through `current_app_user_id()` ->
-- `users.external_auth_id`. Until now that mirroring lived in Python
-- (`telemetry/auth.py`'s `_mirror_user`), which meant it only ever happened
-- if the signup went through Streamlit.
--
-- That is fine while Streamlit is the only client. It stops being fine the
-- moment a browser signs up against GoTrue directly: the account
-- authenticates perfectly, `auth.uid()` returns a real UUID, and every single
-- policy still resolves it to NULL -- so the user is definitely logged in and
-- definitely sees nothing, with no error anywhere. The Next.js app cannot fix
-- this itself either: it holds only the anon key, and there is deliberately
-- no INSERT policy on `users`.
--
-- So the mirroring moves to where every client passes through regardless of
-- which one it is -- including the iOS app that comes later.
--
-- Additive: this creates a trigger and two functions. It does not alter or
-- delete any existing row, and it changes nothing for accounts that already
-- have an `external_auth_id`.

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
-- Pinned: this runs as the definer (postgres) on behalf of
-- `supabase_auth_admin`, so an attacker-controlled search_path would be a
-- privilege-escalation route.
SET search_path = public
AS $fn$
DECLARE
    v_user_id     BIGINT;
    v_linked      TEXT;
    v_display     TEXT;
    v_dob         TEXT;
    v_dob_date    DATE;
    v_guardian    TEXT;
    v_consent     TEXT := 'not_required';
BEGIN
    -- Phone-only signups have no email; this schema has no way to represent
    -- one, so leave it alone rather than inventing a placeholder address.
    IF NEW.email IS NULL THEN
        RETURN NEW;
    END IF;

    v_display  := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'display_name', '')), '');
    v_dob      := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'date_of_birth', '')), '');
    v_guardian := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'guardian_email', '')), '');
    v_display  := COALESCE(v_display, lower(NEW.email));

    -- Mirrors telemetry/accounts.py: `is_minor()` + PARENTAL_CONSENT_AGE=16,
    -- and `create_user()`'s "minor => consent pending". Keep the two in sync;
    -- getting this wrong lets a minor's telemetry be shared without a
    -- guardian having agreed, which `visibility_gate()` exists to prevent.
    IF v_dob IS NOT NULL THEN
        BEGIN
            v_dob_date := v_dob::date;
        EXCEPTION WHEN others THEN
            -- An unparseable DOB is treated as unknown, exactly as
            -- `is_minor()` does, rather than failing the signup.
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
        -- An account already exists for this address. Either it is a
        -- Streamlit-era local-auth account crossing over (external_auth_id
        -- NULL -> adopt it, so the driver keeps their sessions), or it
        -- belongs to a different Supabase identity, in which case adopting
        -- it would hand one person's telemetry to another.
        IF v_linked IS NOT NULL AND v_linked <> NEW.id::text THEN
            RAISE EXCEPTION 'That email address is already linked to a different account.';
        END IF;
        UPDATE users
           SET external_auth_id = NEW.id::text
         WHERE id = v_user_id AND external_auth_id IS NULL;
    ELSE
        INSERT INTO users (
            email, external_auth_id, email_verified, display_name,
            date_of_birth, guardian_email, guardian_consent_status, created_at
        ) VALUES (
            lower(NEW.email), NEW.id::text, NEW.email_confirmed_at IS NOT NULL, v_display,
            v_dob, v_guardian, v_consent, now()
        )
        RETURNING id INTO v_user_id;
    END IF;

    -- The self-registration case creates the driver profile too, claimed
    -- from the outset -- same as `register_user_with_profile()`. Without one
    -- the account cannot own a session, appear on a leaderboard, or join a
    -- team.
    IF NOT EXISTS (SELECT 1 FROM driver_profiles WHERE user_id = v_user_id) THEN
        INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at)
        VALUES (v_display, v_user_id, 'claimed', now(), now());
    END IF;

    RETURN NEW;
END;
$fn$;

-- Verification state lives in Supabase Auth; this only keeps the local
-- mirror's copy honest, so a user who confirms by email link doesn't stay
-- unverified in the app forever.
CREATE OR REPLACE FUNCTION public.handle_auth_user_confirmed()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
BEGIN
    UPDATE users SET email_verified = TRUE WHERE external_auth_id = NEW.id::text;
    RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_auth_user();

DROP TRIGGER IF EXISTS on_auth_user_confirmed ON auth.users;
CREATE TRIGGER on_auth_user_confirmed
    AFTER UPDATE OF email_confirmed_at ON auth.users
    FOR EACH ROW
    WHEN (OLD.email_confirmed_at IS NULL AND NEW.email_confirmed_at IS NOT NULL)
    EXECUTE FUNCTION public.handle_auth_user_confirmed();

$mirror$;

END IF;
END
$$;
