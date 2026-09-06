-- Upload pipeline: a queue row per uploaded file, plus the Storage bucket
-- the raw TSV lands in.
--
-- Streamlit could parse on upload because the browser, the parser and the
-- database all lived in one Python process -- `st.file_uploader` handed the
-- bytes straight to `load_sessions()` and the request simply took ~18s.
-- Once the frontend is a Vercel-hosted Next.js app and parsing is a separate
-- worker, that convenience is gone and the handoff has to be explicit:
--
--   1. client asks for a presigned URL       (POST /api/uploads/presign)
--   2. browser PUTs the file straight to Storage -- never through a
--      serverless function, since a real Unipro export is tens of MB and a
--      million rows, well past typical serverless body limits
--   3. client confirms                        (POST /api/uploads/confirm)
--      -> inserts the `upload_batches` row below with status='pending'
--   4. the worker claims it, parses it, writes sessions/laps, and sets
--      status='complete' (or 'failed' with error_message)
--
-- The frontend polls or subscribes on this row's status to show progress.

CREATE TABLE IF NOT EXISTS upload_batches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Where the raw file landed in the Storage bucket.
    storage_path TEXT NOT NULL,
    original_filename TEXT,
    size_bytes BIGINT,

    -- Who uploaded it, and who the sessions inside it should be filed under.
    -- Two different questions, exactly as `sessions` already separates them:
    -- a team manager uploading a shared logger's file attributes each
    -- session to a different driver. NULL driver_profile_id means "decide
    -- per session on the attribution review screen after parsing".
    uploaded_by_user_id BIGINT REFERENCES users (id),
    driver_profile_id BIGINT REFERENCES driver_profiles (id),

    -- Upload-level context the parser cannot infer, carried through to every
    -- session the file produces (mirrors the Streamlit upload form).
    track_name TEXT,
    session_type TEXT,
    track_condition TEXT,
    temperature_c DOUBLE PRECISION,
    humidity_pct DOUBLE PRECISION,
    pressure_hpa DOUBLE PRECISION,
    altitude_m DOUBLE PRECISION,
    conditions_source TEXT,
    -- Sharing tier applied to the resulting sessions. Defaults to the same
    -- 'shared' the rest of the app defaults to; 'team' and 'private' are the
    -- other tiers (see accounts.VISIBILITY_CHOICES).
    visibility TEXT NOT NULL DEFAULT 'shared',

    -- pending -> processing -> complete | failed
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    sessions_created INTEGER,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- The worker's poll is "oldest pending first", so index for exactly that.
CREATE INDEX IF NOT EXISTS idx_upload_batches_pending
    ON upload_batches (created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_upload_batches_uploader
    ON upload_batches (uploaded_by_user_id);

-- Link each produced session back to the upload it came from, so a failed
-- or duplicated batch can be traced and so the review screen can list
-- "sessions from this upload". Nullable: every session that predates this
-- pipeline has no batch, and sessions ingested by scripts/ingest.py or
-- unigo_sync still won't.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS upload_batch_id BIGINT REFERENCES upload_batches (id);
CREATE INDEX IF NOT EXISTS idx_sessions_upload_batch ON sessions (upload_batch_id);

-- ---------------------------------------------------------------------------
-- RLS: an uploader sees and creates only their own batches. The worker runs
-- on the service-role connection and bypasses all of this.
-- ---------------------------------------------------------------------------

ALTER TABLE upload_batches ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS upload_batches_select_own ON upload_batches;
CREATE POLICY upload_batches_select_own ON upload_batches
    FOR SELECT USING (uploaded_by_user_id = current_app_user_id());

DROP POLICY IF EXISTS upload_batches_insert_own ON upload_batches;
CREATE POLICY upload_batches_insert_own ON upload_batches
    FOR INSERT WITH CHECK (
        uploaded_by_user_id = current_app_user_id()
        -- A client may only ever enqueue work. Letting it write 'complete'
        -- would let it mark an unparsed file as done; letting it write
        -- 'processing' would let it stall the queue.
        AND status = 'pending'
        AND error_message IS NULL
        AND sessions_created IS NULL
    );

-- ---------------------------------------------------------------------------
-- Storage bucket for the raw uploads.
--
-- Guarded: a real Supabase project has the `storage` schema, a plain local
-- Postgres (used by tests/test_rls_policies.py) does not, and this file has
-- to apply cleanly to both.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage') THEN
        -- Private bucket: objects are reachable only via a signed URL or the
        -- service role, never by guessing a public path.
        INSERT INTO storage.buckets (id, name, public)
        VALUES ('telemetry', 'telemetry', false)
        ON CONFLICT (id) DO NOTHING;

        -- Uploads are namespaced by the uploader's auth uid -- `<uid>/<uuid>.tsv`
        -- -- so "the first path segment is my own uid" is the whole ownership
        -- rule, and one driver cannot write into another's folder or read
        -- back their raw telemetry.
        EXECUTE $pol$
            DROP POLICY IF EXISTS telemetry_insert_own_folder ON storage.objects;
            CREATE POLICY telemetry_insert_own_folder ON storage.objects
                FOR INSERT TO authenticated
                WITH CHECK (
                    bucket_id = 'telemetry'
                    AND (storage.foldername(name))[1] = auth.uid()::text
                );

            DROP POLICY IF EXISTS telemetry_select_own_folder ON storage.objects;
            CREATE POLICY telemetry_select_own_folder ON storage.objects
                FOR SELECT TO authenticated
                USING (
                    bucket_id = 'telemetry'
                    AND (storage.foldername(name))[1] = auth.uid()::text
                );
        $pol$;
    END IF;
END
$$;
