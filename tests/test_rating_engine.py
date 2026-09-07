"""End-to-end sanity check for telemetry/rating/engine.py against a real
Postgres -- the SQL wiring is exactly the kind of thing pure-function unit
tests (test_rating_validity/elo/streaks.py) can't catch. Same
skip-if-unreachable shape as test_rls_policies.py.

Requires a local Postgres:
    RLS_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
import subprocess

import pytest

psycopg2 = pytest.importorskip("psycopg2")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")
SIMULATION = os.path.join(REPO, "supabase", "testing", "simulate_supabase.sql")

ADMIN_DSN = os.environ.get("RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
TEST_DB = "rating_engine_test"


def _server_available() -> bool:
    try:
        conn = psycopg2.connect(ADMIN_DSN, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(), reason="no local Postgres reachable (set RLS_TEST_ADMIN_DSN)"
)


def _psql(dsn: str, path: str) -> None:
    result = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"applying {os.path.basename(path)} failed:\n{result.stderr}")


def _straight_lap(base_lat: float, length_m: float, n: int = 300) -> tuple[list[float], list[float], list[float]]:
    """distance_m/latitude/longitude for a straight synthetic lap -- no
    track-cut deviation across any driver here, so validity classification
    in this test is only exercising the "clean lap" path."""
    distance = [i * (length_m / (n - 1)) for i in range(n)]
    lat = [base_lat + d / 111_320.0 for d in distance]
    lon = [0.0] * n
    return distance, lat, lon


@pytest.fixture(scope="module")
def dsn():
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
    return dsn


@pytest.fixture(scope="module")
def seeded(dsn):
    """Three drivers, one shared verified session each, same track/date/
    conditions/class -- a real field-based cohort (3 >= 1 + min_cohort_others).

    Module-scoped and seeded once: several tests below call
    `run_rating_batch()` against this same data (one to check the first
    pass's effects, one to check a second pass changes nothing), which is
    the point -- re-seeding per test would defeat the idempotency check.
    """
    old_url = os.environ.get("SUPABASE_DB_URL")
    os.environ["SUPABASE_DB_URL"] = dsn
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()

    driver_ids = {}
    # Alice fastest, Bob mid, Carol slowest -- reference-line building also
    # needs enough clean laps, so give everyone several laps.
    lap_times = {"Alice": 29.0, "Bob": 30.0, "Carol": 31.0}
    for i, name in enumerate(lap_times):
        cur.execute(
            "INSERT INTO users (email, display_name, created_at) VALUES (%s, %s, now()) RETURNING id",
            (f"{name.lower()}@example.com", name),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at) "
            "VALUES (%s, %s, 'claimed', now(), now()) RETURNING id",
            (name, user_id),
        )
        driver_ids[name] = cur.fetchone()[0]

    for name, lap_time in lap_times.items():
        cur.execute(
            """
            INSERT INTO sessions
                (track_name, start_date, driver_profile_id, best_lap_s, average_lap_s, n_laps,
                 track_condition, engine_category, visibility, attribution_status)
            VALUES (%s, '01-06-2026', %s, %s, %s, 3, 'Dry', 'Senior', 'shared', 'confirmed')
            RETURNING id
            """,
            ("Test Kart Club", driver_ids[name], lap_time, lap_time + 0.5),
        )
        session_id = cur.fetchone()[0]

        for lap_number in range(1, 4):
            this_lap_time = lap_time + (0.3 if lap_number != 2 else 0.0)  # lap 2 is each driver's best
            cur.execute(
                "INSERT INTO laps (session_db_id, lap_number, lap_time_s, is_outlier, outlier_reason) "
                "VALUES (%s, %s, %s, FALSE, NULL)",
                (session_id, lap_number, this_lap_time),
            )
            distance, lat, lon = _straight_lap(base_lat=45.0 + driver_ids[name] * 1e-6, length_m=800.0)
            speed = [80.0] * len(distance)
            braking = [False] * len(distance)
            trace_time = [this_lap_time * i / (len(distance) - 1) for i in range(len(distance))]
            cur.execute(
                """
                INSERT INTO lap_traces (session_db_id, lap_number, sample_count, distance_m, lap_time_s, speed_kmh, latitude, longitude, braking)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (session_id, lap_number, len(distance), distance, trace_time, speed, lat, lon, braking),
            )

    conn.close()
    try:
        yield dsn, driver_ids
    finally:
        if old_url is None:
            os.environ.pop("SUPABASE_DB_URL", None)
        else:
            os.environ["SUPABASE_DB_URL"] = old_url


def test_validity_gate_verifies_clean_laps(seeded):
    dsn, driver_ids = seeded
    from telemetry.rating.engine import run_rating_batch

    result = run_rating_batch()
    assert result["laps_validity_checked"] == 9  # 3 drivers x 3 laps

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT validity_status, COUNT(*) FROM laps GROUP BY validity_status")
        counts = dict(cur.fetchall())
    conn.close()
    assert counts.get("verified", 0) == 9
    assert counts.get("excluded", 0) == 0
    assert counts.get("flagged", 0) is None or counts.get("flagged", 0) == 0


def test_field_based_ratings_favour_the_fastest_driver(seeded):
    dsn, driver_ids = seeded
    from telemetry.rating.engine import run_rating_batch

    run_rating_batch()

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT driver_profile_id, mu, sessions_rated_count FROM driver_ratings")
        ratings = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        cur.execute("SELECT mechanism, cohort_size FROM driver_rating_history")
        history = cur.fetchall()
    conn.close()

    alice_mu = ratings[driver_ids["Alice"]][0]
    bob_mu = ratings[driver_ids["Bob"]][0]
    carol_mu = ratings[driver_ids["Carol"]][0]
    assert alice_mu > bob_mu > carol_mu
    assert all(mechanism == "field" and cohort_size == 3 for mechanism, cohort_size in history)
    assert all(count == 1 for _, count in ratings.values())


def test_rerunning_the_batch_is_idempotent(seeded):
    dsn, driver_ids = seeded
    from telemetry.rating.engine import run_rating_batch

    run_rating_batch()
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT mu FROM driver_ratings WHERE driver_profile_id = %s", (driver_ids["Alice"],))
        mu_after_first_run = cur.fetchone()[0]
    conn.close()

    result = run_rating_batch()
    assert result["rating_updates"] == 0  # nothing new to rate

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT mu FROM driver_ratings WHERE driver_profile_id = %s", (driver_ids["Alice"],))
        mu_after_second_run = cur.fetchone()[0]
    conn.close()
    assert mu_after_first_run == mu_after_second_run


def test_streaks_recorded_for_this_weeks_qualifying_drivers(seeded):
    dsn, driver_ids = seeded
    from telemetry.rating.engine import run_rating_batch

    run_rating_batch()
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT driver_profile_id, current_streak FROM driver_streaks")
        streaks = dict(cur.fetchall())
    conn.close()
    assert streaks[driver_ids["Alice"]] == 1
    assert streaks[driver_ids["Bob"]] == 1
    assert streaks[driver_ids["Carol"]] == 1
