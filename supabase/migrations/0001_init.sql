-- Karting telemetry -- initial Supabase/Postgres schema.
--
-- This is the canonical schema for the Postgres-backed data layer
-- (telemetry/storage.py, accounts.py, auth.py, mailer.py's Supabase*
-- classes -- see telemetry/db.py). Apply it once per project, via
-- `supabase db push` (Supabase CLI) or by pasting it into the project's
-- SQL editor. The Python classes deliberately do NOT run
-- `CREATE TABLE IF NOT EXISTS` themselves the way their SQLite
-- counterparts do: several tables below declare foreign keys across each
-- other, and a shared database that more than one process (this app, a
-- CLI ingest run, and eventually a native mobile app talking to Supabase's
-- REST API directly) can construct, so schema ownership has to live in one
-- place rather than in whichever Python class happens to be instantiated
-- first.
--
-- Table/column shapes mirror the SQLite schemas in the modules named
-- above as closely as possible, so the two backends stay drop-in
-- compatible. Differences, and why:
--   * `INTEGER PRIMARY KEY AUTOINCREMENT` -> `BIGINT GENERATED ALWAYS AS
--     IDENTITY PRIMARY KEY`.
--   * timestamp columns stored as ISO-8601 TEXT in SQLite become
--     `TIMESTAMPTZ` here -- Postgres has a real type for this and
--     PostgREST/a Swift client can consume it directly.
--   * `sessions.cache_path` (a local-disk pickle path) does not exist here.
--     A local pickle path only ever made sense pointing at a filesystem
--     the app itself controls -- which is exactly the assumption that
--     breaks on a redeploy of an app with no persistent disk (the whole
--     reason for this migration). The per-session raw telemetry dataframe
--     instead lives in the new `session_cache` table below, as a Parquet
--     blob (not a pickle -- Parquet is a safe, cross-language columnar
--     format a future non-Python client could also read, whereas
--     unpickling arbitrary bytes is a code-execution surface once the
--     store is reachable over a network rather than only ever written by
--     this one process).
--
-- Row Level Security: this app's own Postgres connection (SUPABASE_DB_URL)
-- is expected to use a role that bypasses RLS (the project's default
-- `postgres`/service-role connection) -- the Python data-access classes
-- already implement the same visibility predicate
-- (`accounts.PUBLIC_VISIBILITY_SQL`) explicitly, and enforcing it twice in
-- the one process that already enforces it correctly would just be
-- redundant. RLS below exists for the case this migration is explicitly
-- building toward: a native client (e.g. an iOS app) querying Supabase's
-- PostgREST API directly with a user's own JWT, as `anon`/`authenticated`,
-- which has no Python code in front of it to apply that predicate at all.
-- Review these policies against your actual Supabase auth setup before
-- relying on them in production -- they are a reasonable first pass, not
-- an audited security boundary.

-- ---------------------------------------------------------------------------
-- Accounts (telemetry/accounts.py)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    -- Supabase Auth's own user id (a UUID, as text) when that's the active
    -- auth backend. NULL for accounts created by the offline/local auth
    -- backend.
    external_auth_id TEXT UNIQUE,
    -- Only ever populated by the local auth backend.
    password_hash TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    display_name TEXT,
    date_of_birth TEXT,
    guardian_email TEXT,
    guardian_consent_status TEXT NOT NULL DEFAULT 'not_required',
    created_at TIMESTAMPTZ NOT NULL,
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS driver_profiles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    display_name TEXT NOT NULL,
    user_id BIGINT UNIQUE REFERENCES users (id),
    claim_status TEXT NOT NULL DEFAULT 'unclaimed',
    invite_email TEXT,
    claim_token TEXT UNIQUE,
    claim_token_expires_at TIMESTAMPTZ,
    created_by_user_id BIGINT REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL,
    claimed_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Session library (telemetry/storage.py)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file TEXT,
    session_index INTEGER,
    driver TEXT,
    track_name TEXT,
    session_type TEXT,
    start_date TEXT,
    start_time TEXT,
    ingested_at TIMESTAMPTZ,
    best_lap_s DOUBLE PRECISION,
    average_lap_s DOUBLE PRECISION,
    std_dev_s DOUBLE PRECISION,
    n_laps INTEGER,
    track_condition TEXT,
    temperature_c DOUBLE PRECISION,
    humidity_pct DOUBLE PRECISION,
    pressure_hpa DOUBLE PRECISION,
    altitude_m DOUBLE PRECISION,
    conditions_source TEXT,
    driver_profile_id BIGINT REFERENCES driver_profiles (id),
    uploaded_by_user_id BIGINT REFERENCES users (id),
    visibility TEXT NOT NULL DEFAULT 'shared',
    attribution_status TEXT NOT NULL DEFAULT 'confirmed',
    kart_class TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_driver_profile ON sessions (driver_profile_id);
CREATE INDEX IF NOT EXISTS idx_sessions_track_name ON sessions (track_name);

-- The per-session raw telemetry dataframe, Parquet-encoded -- see the
-- module docstring above for why this replaces a local-disk pickle path.
CREATE TABLE IF NOT EXISTS session_cache (
    session_db_id BIGINT PRIMARY KEY REFERENCES sessions (id) ON DELETE CASCADE,
    dataframe_parquet BYTEA NOT NULL
);

CREATE TABLE IF NOT EXISTS laps (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_db_id BIGINT REFERENCES sessions (id) ON DELETE CASCADE,
    lap_number INTEGER,
    lap_time_s DOUBLE PRECISION,
    is_outlier BOOLEAN,
    outlier_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_laps_session ON laps (session_db_id);

CREATE TABLE IF NOT EXISTS kart_setups (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_file TEXT,
    session_index INTEGER,
    start_time TEXT,
    driver TEXT,
    saved_at TIMESTAMPTZ,
    setup_json JSONB
);

CREATE TABLE IF NOT EXISTS corner_metrics (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_db_id BIGINT REFERENCES sessions (id) ON DELETE CASCADE,
    driver TEXT,
    track_name TEXT,
    conditions TEXT,
    lap_number INTEGER,
    corner_label TEXT,
    entry_distance_m DOUBLE PRECISION,
    entry_speed_kmh DOUBLE PRECISION,
    apex_distance_m DOUBLE PRECISION,
    apex_speed_kmh DOUBLE PRECISION,
    exit_distance_m DOUBLE PRECISION,
    exit_speed_kmh DOUBLE PRECISION,
    zone_a_time_s DOUBLE PRECISION,
    zone_b_time_s DOUBLE PRECISION,
    zone_c_time_s DOUBLE PRECISION,
    recorded_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pattern_instances (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    driver TEXT,
    track_name TEXT,
    conditions TEXT,
    session_db_id BIGINT REFERENCES sessions (id) ON DELETE CASCADE,
    lap_number INTEGER,
    reference_session_db_id BIGINT REFERENCES sessions (id),
    reference_lap_number INTEGER,
    corner_label TEXT,
    pattern_type TEXT,
    confidence TEXT,
    net_time_impact_s DOUBLE PRECISION,
    evidence_json JSONB,
    recorded_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Attribution / claiming (telemetry/accounts.py, continued -- these
-- reference `sessions`, so they're declared after it)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS attribution_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_db_id BIGINT NOT NULL REFERENCES sessions (id),
    target_driver_profile_id BIGINT NOT NULL REFERENCES driver_profiles (id),
    requested_by_user_id BIGINT REFERENCES users (id),
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS profile_claim_requests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    driver_profile_id BIGINT NOT NULL REFERENCES driver_profiles (id),
    requested_by_user_id BIGINT NOT NULL REFERENCES users (id),
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS attribution_reports (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_db_id BIGINT REFERENCES sessions (id),
    driver_profile_id BIGINT REFERENCES driver_profiles (id),
    reported_by_user_id BIGINT REFERENCES users (id),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Auth (telemetry/auth.py) -- only used when the *local* auth backend is
-- active; when SupabaseAuthProvider is active, Supabase Auth (GoTrue) owns
-- sessions and these tables stay empty. Kept as real tables (rather than
-- dropped) so switching auth backends doesn't require a schema change.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS auth_tokens (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    kind TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users (id),
    token TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- Mailer (telemetry/mailer.py)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS email_outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT,
    sent BOOLEAN NOT NULL DEFAULT FALSE,
    suppressed_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Row Level Security -- see the note at the top of this file: this guards
-- direct client access (e.g. a future iOS app via Supabase's REST API),
-- not this app's own server-side connection.
-- ---------------------------------------------------------------------------

-- Maps the currently-authenticated Supabase Auth user (auth.uid(), a UUID)
-- to this schema's internal, integer `users.id` -- every policy below is
-- written in terms of this rather than repeating the join everywhere.
CREATE OR REPLACE FUNCTION current_app_user_id() RETURNS BIGINT
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT id FROM users WHERE external_auth_id = auth.uid()::text;
$$;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE laps ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE kart_setups ENABLE ROW LEVEL SECURITY;
ALTER TABLE corner_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE pattern_instances ENABLE ROW LEVEL SECURITY;

-- A user may read their own account row; nothing else about `users` (email,
-- password_hash) should be readable by anyone else.
CREATE POLICY users_select_self ON users
    FOR SELECT USING (id = current_app_user_id());

-- Driver profiles: a claimed profile's own owner can read it; anyone
-- authenticated can look up any *claimed* profile (needed for "search
-- registered drivers to attribute a session to" and leaderboard display
-- names) -- unclaimed/invited profiles are visible only to their creator,
-- mirroring that an unclaimed profile is not yet public in any way.
CREATE POLICY driver_profiles_select ON driver_profiles
    FOR SELECT USING (
        claim_status = 'claimed'
        OR user_id = current_app_user_id()
        OR created_by_user_id = current_app_user_id()
    );

-- Sessions: this is `accounts.PUBLIC_VISIBILITY_SQL`, translated 1:1, OR'd
-- with "it's mine" (owning driver profile, or I uploaded it).
CREATE POLICY sessions_select ON sessions
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = sessions.driver_profile_id
              AND sessions.visibility = 'shared'
              AND sessions.attribution_status = 'confirmed'
              AND p.claim_status = 'claimed'
              AND p.user_id IS NOT NULL
        )
        OR uploaded_by_user_id = current_app_user_id()
        OR EXISTS (
            SELECT 1 FROM driver_profiles p
            WHERE p.id = sessions.driver_profile_id AND p.user_id = current_app_user_id()
        )
    );

-- Laps / cached dataframe / corner metrics / pattern instances all inherit
-- their session's own visibility rather than repeating the predicate.
CREATE POLICY laps_select ON laps
    FOR SELECT USING (EXISTS (SELECT 1 FROM sessions s WHERE s.id = laps.session_db_id));

CREATE POLICY session_cache_select ON session_cache
    FOR SELECT USING (EXISTS (SELECT 1 FROM sessions s WHERE s.id = session_cache.session_db_id));

CREATE POLICY corner_metrics_select ON corner_metrics
    FOR SELECT USING (EXISTS (SELECT 1 FROM sessions s WHERE s.id = corner_metrics.session_db_id));

CREATE POLICY pattern_instances_select ON pattern_instances
    FOR SELECT USING (EXISTS (SELECT 1 FROM sessions s WHERE s.id = pattern_instances.session_db_id));

-- Kart setups have no direct FK to `sessions.id` (matched by
-- source_file/session_index/start_time instead -- see storage.py), so
-- there's no session row to join against here. Restrict to the uploading
-- driver's own setups for now; revisit if setups need to be shared the way
-- sessions are.
CREATE POLICY kart_setups_select ON kart_setups
    FOR SELECT USING (driver = (SELECT display_name FROM driver_profiles WHERE user_id = current_app_user_id()));
