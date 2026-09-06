"""Tests for telemetry/db.py's connection helper.

Specifically the shape of what reaches libpq. psycopg2 folds keyword
arguments into the connection string verbatim and libpq parses
`connect_timeout` as an integer, so a float there fails the connection
with `invalid integer value "3.0" for connection option "connect_timeout"`.
That failure looks exactly like an unreachable database to every caller,
which is how it went unnoticed: `unigo_sync.core.connectivity.is_online()`
simply never returned True on a Postgres deployment, so every synced
session queued for later and the queue never drained.

psycopg2 itself is not needed (or installed) to check this -- a stub in
sys.modules records what it was handed.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry import db as pgdb  # noqa: E402


class _FakeConnection:
    def close(self):
        pass


@pytest.fixture
def recorded_connect(monkeypatch):
    """Stub psycopg2 in sys.modules and return the kwargs it was given."""
    recorded = {}

    fake = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object()

    def fake_connect(dsn, **kwargs):
        recorded["dsn"] = dsn
        recorded["kwargs"] = kwargs
        return _FakeConnection()

    fake.connect = fake_connect
    fake.extras = fake_extras

    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pw@example:6543/postgres")
    return recorded


def test_float_timeout_reaches_libpq_as_an_integer(recorded_connect):
    with pgdb.connect(connect_timeout_s=3.0):
        pass

    timeout = recorded_connect["kwargs"]["connect_timeout"]
    assert isinstance(timeout, int) and not isinstance(timeout, bool)
    assert timeout == 3


def test_sub_second_timeout_still_asks_for_a_real_wait(recorded_connect):
    """Rounded, not truncated: int(0.4) is 0, and libpq reads 0 as "wait
    forever" -- the exact opposite of what a short probe timeout means."""
    with pgdb.connect(connect_timeout_s=0.4):
        pass

    assert recorded_connect["kwargs"]["connect_timeout"] == 1


def test_no_timeout_is_not_passed_at_all(recorded_connect):
    with pgdb.connect():
        pass

    assert "connect_timeout" not in recorded_connect["kwargs"]


def test_the_connectivity_probe_timeout_survives_the_round_trip(recorded_connect):
    """The value `unigo_sync.core.connectivity` actually passes must come
    out as a valid libpq integer -- this is the combination that failed."""
    from unigo_sync.core import connectivity

    with pgdb.connect(connect_timeout_s=connectivity._PROBE_TIMEOUT_S):
        pass

    assert isinstance(recorded_connect["kwargs"]["connect_timeout"], int)
    assert recorded_connect["kwargs"]["connect_timeout"] >= 1
