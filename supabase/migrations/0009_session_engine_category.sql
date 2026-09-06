-- ---------------------------------------------------------------------------
-- 0009: the engine a session was actually run with.
--
-- Additive. One new nullable column on `sessions`, one BEFORE INSERT trigger
-- that fills it, and a one-time backfill of that new column. No existing
-- column is read differently, rewritten, or dropped.
--
-- Why on the session rather than read live from `users.engine_category`:
--
--   1. It is history. A driver moving from Junior to Senior would otherwise
--      have every session they ever ran silently relabelled as Senior --
--      including the ones whose lap times only make sense against a Junior
--      engine.
--   2. `users_select_self` (0001) means one driver cannot read another's
--      `users` row at all, so a teammate's engine would render blank on Home
--      while your own rendered fine. Denormalising it onto the session puts
--      it under `sessions_select`, which is the visibility rule that already
--      decides whether you may see the session at all.
--
-- No GRANT is needed: unlike `laps` and `users`, `sessions` has no
-- column-scoped UPDATE grant -- `sessions_update_own` (0002) is the whole
-- boundary, and it already lets a driver correct their own rows.
-- ---------------------------------------------------------------------------

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS engine_category TEXT;

-- ---------------------------------------------------------------------------
-- Filled at insert, for every ingestion path at once.
--
-- Deliberately a trigger rather than a line in the Python: there are three
-- ways a session reaches this table (the upload worker, the unigo_sync
-- bridge, and scripts/ingest.py) and the last time a step lived in only one
-- of them, sessions synced from the logger arrived unanalysed and nothing
-- noticed until a driver asked why the page wanted a script run. One rule in
-- the database cannot be added to two of the three.
--
-- SECURITY DEFINER because the row it reads is in `users`, which
-- `users_select_self` hides from everyone but its owner -- including from
-- the worker inserting on their behalf. It copies a driver's own engine onto
-- a driver's own session and can do nothing else.
--
-- Only ever fills a NULL: an explicit value from the uploader wins.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.fill_session_engine_category()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NEW.engine_category IS NULL AND NEW.driver_profile_id IS NOT NULL THEN
        SELECT u.engine_category
          INTO NEW.engine_category
          FROM driver_profiles p
          JOIN users u ON u.id = p.user_id
         WHERE p.id = NEW.driver_profile_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS sessions_fill_engine_category ON sessions;
CREATE TRIGGER sessions_fill_engine_category
    BEFORE INSERT ON sessions
    FOR EACH ROW
    EXECUTE FUNCTION public.fill_session_engine_category();

-- ---------------------------------------------------------------------------
-- Backfill, once.
--
-- Touches only the column added above, and only where it is still NULL, so
-- re-running this migration is a no-op and nothing already stored changes
-- meaning. Sessions whose driver has not set an engine class stay NULL --
-- "not recorded", which is the truth for every session uploaded before this.
-- ---------------------------------------------------------------------------

UPDATE sessions s
   SET engine_category = u.engine_category
  FROM driver_profiles p
  JOIN users u ON u.id = p.user_id
 WHERE s.driver_profile_id = p.id
   AND s.engine_category IS NULL
   AND u.engine_category IS NOT NULL;
