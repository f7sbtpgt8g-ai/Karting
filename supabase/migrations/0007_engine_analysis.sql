-- Engine analysis: what class the driver runs, and the per-lap engine
-- figures the engine page is built from.
--
-- Additive: one column on `users`, eight on `lap_traces`, two new arrays,
-- and one narrowly-scoped UPDATE grant.

-- ---------------------------------------------------------------------------
-- Engine category.
--
-- On `users` rather than on a session: it is a property of what the driver
-- races, changes maybe once a season, and is needed before any session
-- exists (it is asked at registration). A driver who moves class mid-season
-- re-labels every past session by changing it, which is the behaviour a
-- single value gives and the honest reading of "what do you drive".
--
-- Free text with no CHECK constraint on purpose: the valid list is a
-- product decision that will gain entries (new Rotax classes, IAME, KZ) and
-- a CHECK would turn each addition into a migration. The app offers the
-- list; the column records the answer.
-- ---------------------------------------------------------------------------

ALTER TABLE users ADD COLUMN IF NOT EXISTS engine_category TEXT;

-- A driver may set their own class, and nothing else on their user row.
-- Column-level, for the same reason as `laps` in 0006: an UPDATE policy
-- decides which rows may be touched, never which columns, and `users` holds
-- `email`, `external_auth_id` and the guardian-consent state.
REVOKE UPDATE ON users FROM anon, authenticated;
GRANT UPDATE (engine_category, display_name) ON users TO authenticated;

DROP POLICY IF EXISTS users_update_own ON users;
CREATE POLICY users_update_own ON users
    FOR UPDATE
    USING (id = current_app_user_id())
    WITH CHECK (id = current_app_user_id());

-- ---------------------------------------------------------------------------
-- Per-lap engine figures.
--
-- Aggregates rather than arrays wherever the page shows a number: the engine
-- table is one row per lap with ten numeric columns, and computing them in
-- the browser would mean shipping every lap's full trace to render a table.
-- ---------------------------------------------------------------------------

ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS min_speed_kmh DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS min_rpm DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS avg_rpm DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS max_temp_c DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS min_temp_c DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS avg_temp_c DOUBLE PRECISION;

-- Share of the lap spent in the Rotax peak-power band. The band itself is
-- `DEFAULT_PEAK_POWER_RPM_BAND` in telemetry/setup_engine.py -- deliberately
-- not repeated here, because the gearing suggestions read the same band and
-- two copies drifting apart would have them disagreeing about where the
-- engine makes power.
--
-- Stored for every lap regardless of class, because it is just "time in an
-- RPM window" and the class only decides whether the number means anything;
-- the page shows it for Rotax and hides it otherwise.
--
-- Measured over samples rather than over time. The RPM channel logs at a
-- steady rate, so the two agree closely, and a time-weighted version would
-- need the sample intervals stored alongside.
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS powerzone_pct DOUBLE PRECISION;

-- ---------------------------------------------------------------------------
-- Trace arrays the comparison charts plot.
--
-- Speed, RPM and elapsed time are already stored (0005). These two complete
-- the set the old Streamlit charts drew: this logger has no brake or throttle
-- channel, so lateral and longitudinal G are how cornering load and
-- braking/acceleration are read at all.
-- ---------------------------------------------------------------------------

ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS lateral_g DOUBLE PRECISION[];
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS longitudinal_g DOUBLE PRECISION[];
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS temp_c DOUBLE PRECISION[];

-- ---------------------------------------------------------------------------
-- Carry the engine class through signup.
--
-- Registration asks for it, so the trigger that creates the local `users`
-- row has to read it -- otherwise the answer is collected, stored in the
-- Supabase identity's metadata, and silently dropped on the floor.
--
-- Redefined rather than patched: CREATE OR REPLACE FUNCTION needs the whole
-- body, and the trigger itself is unchanged. Everything else here is
-- identical to 0004.
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
    v_consent     TEXT := 'not_required';
BEGIN
    IF NEW.email IS NULL THEN
        RETURN NEW;
    END IF;

    v_display  := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'display_name', '')), '');
    v_dob      := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'date_of_birth', '')), '');
    v_guardian := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'guardian_email', '')), '');
    v_engine   := NULLIF(btrim(COALESCE(NEW.raw_user_meta_data ->> 'engine_category', '')), '');
    v_display  := COALESCE(v_display, lower(NEW.email));

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
               -- Only fill a class in, never overwrite one the driver has
               -- already set: this branch also runs for a Streamlit-era
               -- account crossing over.
               engine_category = COALESCE(engine_category, v_engine)
         WHERE id = v_user_id AND external_auth_id IS NULL;
    ELSE
        INSERT INTO users (
            email, external_auth_id, email_verified, display_name,
            date_of_birth, guardian_email, guardian_consent_status,
            engine_category, created_at
        ) VALUES (
            lower(NEW.email), NEW.id::text, NEW.email_confirmed_at IS NOT NULL, v_display,
            v_dob, v_guardian, v_consent, v_engine, now()
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
