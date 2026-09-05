#!/usr/bin/env python
"""One-off migration: copy an existing local SQLite session library
(`data/sessions.db` by default) into a Postgres/Supabase database.

Run this once when moving an existing local/offline install over to
Supabase-backed storage (see README's "Migrating the database layer to
Supabase"). Requires `SUPABASE_DB_URL` (or `DATABASE_URL`) to be set to the
target Postgres connection string, and the schema in
`supabase/migrations/0001_init.sql` to already be applied there.

Row ids are preserved across the copy (rather than re-inserted and
reassigned) so that every foreign key -- `laps.session_db_id`,
`corner_metrics.session_db_id`, `attribution_requests.target_driver_profile_id`,
and so on -- still points at the right row afterwards; the identity
sequence for each table is then advanced past the highest copied id so
new rows inserted by the app afterwards don't collide with migrated ones.

Usage:
    SUPABASE_DB_URL=postgresql://... python scripts/migrate_sqlite_to_supabase.py \
        --sqlite-db data/sessions.db
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys

import pandas as pd

from telemetry import db as pgdb

# (table name, ordered column list, whether the table has a plain integer
# `id` identity column to preserve/advance). Order matters: a table must
# come after every table it has a foreign key into.
TABLES: list[tuple[str, list[str], bool]] = [
    ("users", [
        "id", "email", "external_auth_id", "password_hash", "email_verified", "display_name",
        "date_of_birth", "guardian_email", "guardian_consent_status", "created_at", "last_login_at",
    ], True),
    ("driver_profiles", [
        "id", "display_name", "user_id", "claim_status", "invite_email", "claim_token",
        "claim_token_expires_at", "created_by_user_id", "created_at", "claimed_at",
    ], True),
    ("sessions", [
        "id", "source_file", "session_index", "driver", "track_name", "session_type", "start_date",
        "start_time", "ingested_at", "best_lap_s", "average_lap_s", "std_dev_s", "n_laps",
        "track_condition", "temperature_c", "humidity_pct", "pressure_hpa", "altitude_m",
        "conditions_source", "driver_profile_id", "uploaded_by_user_id", "visibility",
        "attribution_status", "kart_class",
    ], True),
    ("laps", ["id", "session_db_id", "lap_number", "lap_time_s", "is_outlier", "outlier_reason"], True),
    ("kart_setups", ["id", "source_file", "session_index", "start_time", "driver", "saved_at", "setup_json"], True),
    ("corner_metrics", [
        "id", "session_db_id", "driver", "track_name", "conditions", "lap_number", "corner_label",
        "entry_distance_m", "entry_speed_kmh", "apex_distance_m", "apex_speed_kmh", "exit_distance_m",
        "exit_speed_kmh", "zone_a_time_s", "zone_b_time_s", "zone_c_time_s", "recorded_at",
    ], True),
    ("pattern_instances", [
        "id", "driver", "track_name", "conditions", "session_db_id", "lap_number",
        "reference_session_db_id", "reference_lap_number", "corner_label", "pattern_type",
        "confidence", "net_time_impact_s", "evidence_json", "recorded_at",
    ], True),
    ("attribution_requests", [
        "id", "session_db_id", "target_driver_profile_id", "requested_by_user_id", "status",
        "message", "created_at", "resolved_at",
    ], True),
    ("profile_claim_requests", [
        "id", "driver_profile_id", "requested_by_user_id", "status", "note", "created_at", "resolved_at",
    ], True),
    ("attribution_reports", [
        "id", "session_db_id", "driver_profile_id", "reported_by_user_id", "reason", "status", "created_at",
    ], True),
    ("auth_tokens", ["id", "user_id", "kind", "token", "expires_at", "used_at", "created_at"], True),
    ("auth_sessions", ["id", "user_id", "token", "created_at", "expires_at", "revoked_at"], True),
    ("email_outbox", [
        "id", "to_email", "subject", "body", "kind", "sent", "suppressed_reason", "created_at",
    ], True),
]

# Columns that are stored as 0/1 in SQLite but BOOLEAN in Postgres.
BOOLEAN_COLUMNS = {"email_verified", "is_outlier", "sent"}
# Columns that are stored as a JSON string in SQLite but JSONB in Postgres --
# passed through as text; Postgres casts a well-formed JSON string literal
# to jsonb on assignment, so no explicit parsing is needed here.


def copy_table(sqlite_conn: sqlite3.Connection, pg_conn, table: str, columns: list[str], has_id: bool) -> int:
    existing = {row[1] for row in sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if not existing:
        print(f"  (source has no {table!r} table -- skipping)")
        return 0
    cols = [c for c in columns if c in existing]
    rows = sqlite_conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: 0 rows")
        return 0

    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(cols)
    overriding = " OVERRIDING SYSTEM VALUE" if has_id and "id" in cols else ""
    insert_sql = f"INSERT INTO {table} ({col_list}){overriding} VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"

    cur = pg_conn.cursor()
    n = 0
    for row in rows:
        values = []
        for col, value in zip(cols, row):
            if col in BOOLEAN_COLUMNS and value is not None:
                value = bool(value)
            values.append(value)
        cur.execute(insert_sql, values)
        n += 1
    if has_id and "id" in cols:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"GREATEST((SELECT COALESCE(MAX(id), 1) FROM {table}), 1))"
        )
    pg_conn.commit()
    print(f"  {table}: {n} row(s) copied")
    return n


def copy_session_caches(sqlite_conn: sqlite3.Connection, pg_conn) -> int:
    """Sessions' raw dataframe pickles live on local disk (`cache_path`),
    not in a SQLite column -- read each one, re-encode as Parquet (see
    storage.SupabaseSessionLibrary's module docstring for why not pickle),
    and insert into the new `session_cache` table."""
    rows = sqlite_conn.execute("SELECT id, cache_path FROM sessions WHERE cache_path IS NOT NULL").fetchall()
    cur = pg_conn.cursor()
    n = 0
    for session_id, cache_path in rows:
        if not cache_path or not os.path.exists(cache_path):
            print(f"  WARNING: session {session_id}'s cache file {cache_path!r} not found -- skipping its dataframe", file=sys.stderr)
            continue
        df = pd.read_pickle(cache_path)
        buf = io.BytesIO()
        df.to_parquet(buf, engine="pyarrow")
        cur.execute(
            "INSERT INTO session_cache (session_db_id, dataframe_parquet) VALUES (%s, %s) "
            "ON CONFLICT (session_db_id) DO NOTHING",
            (session_id, buf.getvalue()),
        )
        n += 1
    pg_conn.commit()
    print(f"  session_cache: {n} dataframe(s) copied")
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite-db", default="data/sessions.db", help="Path to the existing SQLite session library")
    args = parser.parse_args(argv)

    if not os.path.exists(args.sqlite_db):
        print(f"No SQLite database found at {args.sqlite_db!r} -- nothing to migrate.", file=sys.stderr)
        return 1
    if not pgdb.has_postgres_configured():
        print("SUPABASE_DB_URL (or DATABASE_URL) is not set -- nothing to migrate into.", file=sys.stderr)
        return 1

    sqlite_conn = sqlite3.connect(args.sqlite_db)
    try:
        with pgdb.connect() as pg_conn:
            print(f"Copying {args.sqlite_db} -> {pgdb.database_url().split('@')[-1]} ...")
            for table, columns, has_id in TABLES:
                copy_table(sqlite_conn, pg_conn, table, columns, has_id)
            print("Copying cached session dataframes (pickle -> Parquet)...")
            copy_session_caches(sqlite_conn, pg_conn)
    finally:
        sqlite_conn.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
