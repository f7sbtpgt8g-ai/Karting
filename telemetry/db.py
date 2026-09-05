"""Shared Postgres/Supabase connection helper.

Every Postgres-backed sibling class in `storage.py` / `accounts.py` /
`auth.py` / `mailer.py` connects to the same database (a Supabase project
in production) through this module, rather than each hand-rolling its own
`psycopg2.connect()`. It also centralizes the one dialect difference that
would otherwise need repeating at every call site: SQLite's `col IS ?`
null-safe comparison has no parameterized equivalent in Postgres (`IS` only
accepts a literal `NULL`/`TRUE`/`FALSE`/`DISTINCT FROM`), so
`is_not_distinct_from_sql` is the one place that gets written correctly and
then reused everywhere a nullable column is matched by value.

Unlike the SQLite classes, nothing here runs `CREATE TABLE IF NOT EXISTS`
on connect. A shared Postgres database used by more than one process (this
app, `scripts/ingest.py`, and eventually a native mobile client talking to
it directly over Supabase's REST API) needs one canonical schema applied
once -- see `supabase/migrations/0001_init.sql` -- not four different
Python classes racing to create tables (with foreign keys across each
other) the first time whichever one happens to be constructed first.
"""

from __future__ import annotations

import os
import warnings
from contextlib import contextmanager

# pandas warns on every pd.read_sql_query() call against a raw psycopg2
# connection ("only supports SQLAlchemy connectable ... or sqlite3 DBAPI2
# connection"). That's a deliberate choice here, not an oversight -- adding
# a SQLAlchemy dependency for this one warning isn't worth it given how
# much hand-written SQL already exists in this codebase (dynamic query
# building, PUBLIC_VISIBILITY_SQL) -- so it's suppressed rather than left
# to spam every page load.
warnings.filterwarnings(
    "ignore", message=r"pandas only supports SQLAlchemy connectable.*", category=UserWarning
)


def has_postgres_configured() -> bool:
    """Whether a Postgres/Supabase connection is configured in this
    environment. Every `*_from_env` factory in the data-layer modules uses
    this to decide between the Postgres-backed class and the offline
    SQLite one -- the same shape as `auth.provider_from_env`."""
    return bool(os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL"))


def database_url() -> str:
    """The Postgres connection string -- `SUPABASE_DB_URL` if set
    (matches the "Connection string" shown in a Supabase project's Database
    settings), else `DATABASE_URL` for a plain Postgres instance. Raises
    rather than silently falling back, so a misconfigured deployment fails
    loudly instead of quietly writing to a database nobody's looking at."""
    url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No Postgres connection configured -- set SUPABASE_DB_URL (or DATABASE_URL) to "
            "a Postgres connection string, e.g. the 'Connection string' shown under "
            "Project Settings > Database in your Supabase project."
        )
    return url


@contextmanager
def connect(connect_timeout_s: float | None = None):
    """One short-lived connection per call -- the same convention the
    SQLite classes use (see `SessionLibrary`'s docstring for why a
    long-lived connection shared across Streamlit reruns was found to
    hang). Supabase's own connection pooler (pgbouncer) sits in front of
    this in production, so paying for a fresh connection per call is cheap
    here, not the bottleneck it would be against a bare Postgres instance.

    `connect_timeout_s` is libpq's own `connect_timeout` (seconds to wait
    for the TCP handshake, not the whole session) -- left as the psycopg2
    default (effectively no timeout) unless a caller passes one. A caller
    that just wants to know "is the database reachable right now" (see
    `unigo_sync.core.connectivity`) should pass a short value rather than
    risk the OS's own multi-minute TCP timeout on a network that's simply
    gone, e.g. a laptop joined to a device's own offline WiFi AP.
    """
    import psycopg2
    import psycopg2.extras

    kwargs = {"cursor_factory": psycopg2.extras.RealDictCursor}
    if connect_timeout_s is not None:
        kwargs["connect_timeout"] = connect_timeout_s
    conn = psycopg2.connect(database_url(), **kwargs)
    try:
        yield conn
    finally:
        conn.close()


def read_sql(conn, query: str, params: tuple = ()):
    """`pandas.read_sql_query`, but safe against this module's connections.

    `connect()` hands out connections whose default cursor factory is
    `psycopg2.extras.RealDictCursor`, so every hand-written `conn.cursor()`
    call elsewhere in the Postgres-backed classes gets dict-like rows
    (`row["column"]`) for free. `pd.read_sql_query(query, conn, ...)`
    inherits that same default cursor internally -- but pandas's
    non-SQLAlchemy DBAPI2 fallback path assumes each fetched row is a plain
    positional tuple, and silently produces corrupted columns when handed a
    dict-like row instead (each column's value ends up holding the *next*
    column's name). This works around it by fetching through an explicit
    plain-tuple cursor and building the DataFrame from that directly."""
    import pandas as pd
    import psycopg2.extensions

    # Explicitly overrides the connection's default RealDictCursor factory
    # (set in `connect()` below) -- `conn.cursor()` with no argument would
    # otherwise still inherit it.
    cur = conn.cursor(cursor_factory=psycopg2.extensions.cursor)
    cur.execute(query, params)
    columns = [d[0] for d in cur.description]
    return pd.DataFrame(cur.fetchall(), columns=columns)


def is_not_distinct_from_sql(column: str) -> str:
    """`column IS NOT DISTINCT FROM %s` -- the parameterized, Postgres-safe
    equivalent of SQLite's `column IS ?`, which matches NULL-to-NULL as
    equal (needed since e.g. a session's `start_time` is frequently NULL
    and still needs to participate in duplicate-detection matching)."""
    return f"{column} IS NOT DISTINCT FROM %s"
