"""Track naming against a real database.

`test_track_naming.py` covers the geometry. This covers the part that talks
to Postgres: that an unnamed session gets named from the tracks already
stored, that a name someone typed is never overwritten, and that a session
at an unrecognised circuit is left alone.

Requires a local Postgres, same as tests/test_rls_policies.py:
    RLS_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
import subprocess

import numpy as np
import pandas as pd
import pytest

psycopg2 = pytest.importorskip("psycopg2")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")
SIMULATION = os.path.join(REPO, "supabase", "testing", "simulate_supabase.sql")

ADMIN_DSN = os.environ.get(
    "RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
TEST_DB = os.environ.get("TRACK_NAMING_TEST_DB", "track_naming_test")

BARMOSEN = (55.0722, 11.7861)
AALBORG = (57.0488, 9.9217)


def _server_available() -> bool:
    try:
        psycopg2.connect(ADMIN_DSN, connect_timeout=3).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(), reason="no local Postgres reachable (set RLS_TEST_ADMIN_DSN)"
)


def _psql(dsn: str, path: str) -> None:
    result = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"applying {os.path.basename(path)} failed:\n{result.stderr}")


@pytest.fixture
def db(monkeypatch):
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    admin.close()

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB
    _psql(dsn, SIMULATION)
    for name in sorted(os.listdir(MIGRATIONS)):
        if name.endswith(".sql"):
            _psql(dsn, os.path.join(MIGRATIONS, name))

    monkeypatch.setenv("SUPABASE_DB_URL", dsn)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn
    conn.close()


class _FakeSession:
    def __init__(self, centre):
        angles = np.linspace(0, 2 * np.pi, 60)
        self._frame = pd.DataFrame(
            {
                "Latitude": [centre[0] + 0.002 * np.cos(a) for a in angles],
                "Longitude": [centre[1] + 0.002 * np.sin(a) for a in angles],
            }
        )

    def gps_fixes(self):
        return self._frame


def _known_track(conn, name: str, centre) -> int:
    """A session that already carries a track name, with a stored trace --
    which is what makes it usable as a position reference."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (source_file, session_index, track_name) "
            "VALUES ('known.tsv', 1, %s) RETURNING id",
            (name,),
        )
        session_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO lap_traces "
            "(session_db_id, lap_number, sample_count, latitude, longitude, distance_m, lap_time_s) "
            "VALUES (%s, 1, 2, %s, %s, %s, %s)",
            (
                session_id,
                [centre[0], centre[0]],
                [centre[1], centre[1]],
                [0.0, 10.0],
                [0.0, 0.5],
            ),
        )
    return session_id


def _unnamed_session(conn, name=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (source_file, session_index, track_name) "
            "VALUES ('synced.tsv', 1, %s) RETURNING id",
            (name,),
        )
        return cur.fetchone()[0]


def _track_name(conn, session_id: int):
    with conn.cursor() as cur:
        cur.execute("SELECT track_name FROM sessions WHERE id = %s", (session_id,))
        return cur.fetchone()[0]


def test_an_unnamed_session_is_named_from_a_track_already_stored(db):
    from telemetry.track_naming import name_track_if_unknown

    _known_track(db, "Barmosen", BARMOSEN)
    session_id = _unnamed_session(db)

    assert name_track_if_unknown(session_id, _FakeSession(BARMOSEN)) == "Barmosen"
    assert _track_name(db, session_id) == "Barmosen"


def test_a_name_someone_typed_is_never_overwritten(db):
    """They were at the track and this function was not. Even if the GPS
    says otherwise, a name a human entered wins."""
    from telemetry.track_naming import name_track_if_unknown

    _known_track(db, "Barmosen", BARMOSEN)
    session_id = _unnamed_session(db, name="Barmosen West Layout")

    assert name_track_if_unknown(session_id, _FakeSession(BARMOSEN)) is None
    assert _track_name(db, session_id) == "Barmosen West Layout"


def test_a_session_at_an_unrecognised_circuit_stays_unnamed(db):
    from telemetry.track_naming import name_track_if_unknown

    _known_track(db, "Barmosen", BARMOSEN)
    session_id = _unnamed_session(db)

    assert name_track_if_unknown(session_id, _FakeSession(AALBORG)) is None
    assert _track_name(db, session_id) is None


def test_nothing_named_yet_leaves_the_session_alone(db):
    from telemetry.track_naming import name_track_if_unknown

    session_id = _unnamed_session(db)
    assert name_track_if_unknown(session_id, _FakeSession(BARMOSEN)) is None
    assert _track_name(db, session_id) is None


def test_the_closest_of_several_known_tracks_wins(db):
    from telemetry.track_naming import name_track_if_unknown

    _known_track(db, "Aalborg", AALBORG)
    _known_track(db, "Barmosen", BARMOSEN)
    session_id = _unnamed_session(db)

    assert name_track_if_unknown(session_id, _FakeSession(BARMOSEN)) == "Barmosen"


def test_a_missing_session_is_not_an_error(db):
    from telemetry.track_naming import name_track_if_unknown

    assert name_track_if_unknown(999_999, _FakeSession(BARMOSEN)) is None


def test_known_locations_lists_one_point_per_named_track(db):
    from telemetry.track_naming import known_track_locations

    _known_track(db, "Barmosen", BARMOSEN)
    _known_track(db, "Barmosen", BARMOSEN)
    _known_track(db, "Aalborg", AALBORG)

    names = [row[0] for row in known_track_locations()]
    assert sorted(names) == ["Aalborg", "Barmosen"]
