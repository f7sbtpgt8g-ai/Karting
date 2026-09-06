-- What the Lap Analysis page needs beyond 0005: a driver's own judgement
-- about which laps count, and the per-lap peaks the summary cards show.
--
-- Additive: two nullable columns, one boolean with a default, one new
-- policy. Nothing existing changes meaning.

-- ---------------------------------------------------------------------------
-- Manual lap exclusion.
--
-- `laps.is_outlier` is the *automatic* judgement (statistical outlier, out-lap,
-- in-lap). This is the driver's own, and it is deliberately a separate column
-- rather than an override of that one: "the algorithm thinks this lap is
-- unrepresentative" and "I went off at turn 3" are different claims, and
-- collapsing them would mean re-analysis silently discarding what the driver
-- said, or the driver's click being indistinguishable from a detection.
-- ---------------------------------------------------------------------------

ALTER TABLE laps ADD COLUMN IF NOT EXISTS excluded_by_user BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE laps ADD COLUMN IF NOT EXISTS exclusion_note TEXT;

-- ---------------------------------------------------------------------------
-- Per-lap peaks, for the summary cards.
--
-- Derivable from `lap_traces.speed_kmh`/`rpm`, but only by pulling every
-- lap's full arrays -- ~300 KB to render two numbers. Stored as scalars by
-- the same writer that fills the arrays.
-- ---------------------------------------------------------------------------

ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS max_speed_kmh DOUBLE PRECISION;
ALTER TABLE lap_traces ADD COLUMN IF NOT EXISTS max_rpm DOUBLE PRECISION;

-- ---------------------------------------------------------------------------
-- RLS: the session's owner may exclude their own laps, and nothing else.
--
-- Column-level grants do the narrowing, because a policy cannot: RLS decides
-- which *rows* an UPDATE may touch, never which columns. Without the REVOKE
-- below, Supabase's default `GRANT ALL` plus this new policy would let a
-- client rewrite `lap_time_s` -- which feeds every leaderboard in the app.
--
-- So: take UPDATE away wholesale, hand back exactly the two columns this
-- feature writes. The worker keeps writing everything on the service-role
-- connection, which bypasses all of it.
-- ---------------------------------------------------------------------------

REVOKE UPDATE ON laps FROM anon, authenticated;
GRANT UPDATE (excluded_by_user, exclusion_note) ON laps TO authenticated;

DROP POLICY IF EXISTS laps_update_own ON laps;
CREATE POLICY laps_update_own ON laps
    FOR UPDATE
    USING (
        EXISTS (
            SELECT 1 FROM sessions s
            WHERE s.id = laps.session_db_id
              AND (
                  s.uploaded_by_user_id = current_app_user_id()
                  OR EXISTS (
                      SELECT 1 FROM driver_profiles p
                      WHERE p.id = s.driver_profile_id AND p.user_id = current_app_user_id()
                  )
              )
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM sessions s
            WHERE s.id = laps.session_db_id
              AND (
                  s.uploaded_by_user_id = current_app_user_id()
                  OR EXISTS (
                      SELECT 1 FROM driver_profiles p
                      WHERE p.id = s.driver_profile_id AND p.user_id = current_app_user_id()
                  )
              )
        )
    );
