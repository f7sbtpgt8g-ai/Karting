-- parse_session_date (0011) only ever matched DD-MM-YYYY, on the assumption
-- that's the one shape sessions.start_date is written in. It isn't: that
-- column is written verbatim from the Unipro export's own "Start Date"
-- field (telemetry/parser.py never reformats it), so its shape follows
-- whatever the logger/locale that produced the export used. A real
-- deployment surfaced this with ISO YYYY-MM-DD dates throughout -- every
-- one of them silently parsed to NULL, which is why "last driven" on the
-- Tracks page reads blank for a driver whose export writes dates that way
-- (telemetry/rating/engine.py's own Python-side copy of this same
-- assumption had the identical bug, fixed separately; this is its SQL
-- counterpart).
--
-- CREATE OR REPLACE keeps every existing grant on this function -- Postgres
-- does not reset a function's ACL on a same-signature replace -- but the
-- REVOKE/GRANT pair is restated anyway, matching this migration history's
-- habit of never leaving a function's security/grants merely inherited.

CREATE OR REPLACE FUNCTION parse_session_date(p_raw TEXT)
RETURNS DATE
LANGUAGE sql
IMMUTABLE
SET search_path = public
AS $$
    SELECT CASE
        WHEN p_raw ~ '^\d{2}-\d{2}-\d{4}$' THEN to_date(p_raw, 'DD-MM-YYYY')
        WHEN p_raw ~ '^\d{4}-\d{2}-\d{2}$' THEN to_date(p_raw, 'YYYY-MM-DD')
        ELSE NULL
    END;
$$;

REVOKE ALL ON FUNCTION parse_session_date(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION parse_session_date(TEXT) TO authenticated;
