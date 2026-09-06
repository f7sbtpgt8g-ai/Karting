"""The admin listing and, mostly, `admin_delete_user`.

This is the most dangerous function in the schema: it is SECURITY DEFINER,
it deletes across nine tables, and it removes Supabase Auth identities. So
the tests are weighted towards what must NOT happen -- a non-admin calling
it, an admin deleting themselves, the last admin going, and collateral
damage to other people's data that happens to reference the deleted user.

Requires a local Postgres, same as tests/test_rls_policies.py.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")
SIMULATION = os.path.join(REPO, "supabase", "testing", "simulate_supabase.sql")

ADMIN_DSN = os.environ.get(
    "RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
TEST_DB = os.environ.get("ADMIN_TEST_DB", "admin_test")


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


class Actor:
    """One authenticated client, as PostgREST presents it."""

    def __init__(self, conn, auth_uid: str | None):
        self.conn = conn
        self.auth_uid = auth_uid

    def call(self, sql: str, params: tuple = ()):
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                claims = (
                    f'{{"sub":"{self.auth_uid}","role":"authenticated"}}'
                    if self.auth_uid
                    else '{"role":"anon"}'
                )
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute(f"SET LOCAL ROLE {'authenticated' if self.auth_uid else 'anon'}")
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
                cur.execute("COMMIT")
                return rows
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def try_call(self, sql: str, params: tuple = ()):
        try:
            return (self.call(sql, params), None)
        except psycopg2.Error as exc:
            return ([], str(exc).splitlines()[0])


@pytest.fixture
def world():
    """An admin, an ordinary driver with data, and a bystander whose own
    data references the driver."""
    admin_conn = psycopg2.connect(ADMIN_DSN)
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    admin_conn.close()

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB
    _psql(dsn, SIMULATION)
    for name in sorted(os.listdir(MIGRATIONS)):
        if name.endswith(".sql"):
            _psql(dsn, os.path.join(MIGRATIONS, name))

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    ids: dict = {}

    with conn.cursor() as cur:
        for name, is_admin in (("boss", True), ("driver", False), ("bystander", False)):
            uid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO auth.users (id, email) VALUES (%s, %s)",
                (uid, f"{name}@example.com"),
            )
            # The signup trigger creates users + driver_profiles.
            cur.execute("SELECT id FROM users WHERE email=%s", (f"{name}@example.com",))
            ids[f"{name}_user"] = cur.fetchone()[0]
            ids[f"{name}_uid"] = uid
            cur.execute("SELECT id FROM driver_profiles WHERE user_id=%s", (ids[f"{name}_user"],))
            ids[f"{name}_profile"] = cur.fetchone()[0]
            if is_admin:
                cur.execute("UPDATE users SET is_admin=TRUE WHERE id=%s", (ids[f"{name}_user"],))
            cur.execute(
                "UPDATE users SET last_login_at=now() WHERE id=%s", (ids[f"{name}_user"],)
            )

        # The driver's own data, with the full downstream chain attached.
        cur.execute(
            "INSERT INTO upload_batches (storage_path, uploaded_by_user_id, driver_profile_id, status) "
            "VALUES ('uid/a.tsv',%s,%s,'complete') RETURNING id",
            (ids["driver_user"], ids["driver_profile"]),
        )
        ids["batch"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sessions (source_file, session_index, driver, track_name, start_date, "
            "ingested_at, best_lap_s, n_laps, driver_profile_id, uploaded_by_user_id, "
            "upload_batch_id, visibility, attribution_status) "
            "VALUES ('a.tsv',0,'Driver','Ring','01-01-2026',now(),30.0,10,%s,%s,%s,'shared','confirmed') "
            "RETURNING id",
            (ids["driver_profile"], ids["driver_user"], ids["batch"]),
        )
        ids["session"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO laps (session_db_id, lap_number, lap_time_s, is_outlier) "
            "VALUES (%s,1,30.0,FALSE)",
            (ids["session"],),
        )
        cur.execute(
            "INSERT INTO session_analysis (session_db_id, best_lap) VALUES (%s,1)",
            (ids["session"],),
        )
        cur.execute(
            "INSERT INTO lap_traces (session_db_id, lap_number, sample_count, distance_m, lap_time_s) "
            "VALUES (%s,1,2,'{0,10}','{0,1}')",
            (ids["session"],),
        )

        # A team the driver founded, with the bystander as a member -- the
        # team must outlive the driver.
        cur.execute(
            "INSERT INTO teams (name, created_by_user_id, created_at) "
            "VALUES ('Reds',%s,now()) RETURNING id",
            (ids["driver_user"],),
        )
        ids["team"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at, "
            "decided_by_user_id) VALUES (%s,%s,'member','active',now(),%s)",
            (ids["team"], ids["bystander_profile"], ids["driver_user"]),
        )

        # An unclaimed profile the driver created for someone else, carrying
        # the bystander's session -- must survive.
        cur.execute(
            "INSERT INTO driver_profiles (display_name, claim_status, created_by_user_id, created_at) "
            "VALUES ('Guest','unclaimed',%s,now()) RETURNING id",
            (ids["driver_user"],),
        )
        ids["guest_profile"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sessions (source_file, session_index, driver, track_name, start_date, "
            "ingested_at, best_lap_s, n_laps, driver_profile_id, uploaded_by_user_id, visibility, "
            "attribution_status) VALUES ('b.tsv',0,'Guest','Ring','01-01-2026',now(),31.0,8,%s,%s,"
            "'shared','confirmed') RETURNING id",
            (ids["guest_profile"], ids["bystander_user"]),
        )
        ids["bystander_session"] = cur.fetchone()[0]

    ids.update(
        {
            "conn": conn,
            "boss": Actor(conn, ids["boss_uid"]),
            "driver": Actor(conn, ids["driver_uid"]),
            "anon": Actor(conn, None),
        }
    )
    yield ids
    conn.close()


# ------------------------------------------------------------- the listing


def test_an_admin_sees_every_account_with_its_stats(world):
    rows = world["boss"].call("SELECT id, email, session_count, upload_count, lap_count FROM admin_user_overview()")
    by_email = {r[1]: r for r in rows}
    assert set(by_email) == {"boss@example.com", "driver@example.com", "bystander@example.com"}
    assert by_email["driver@example.com"][2] == 1, "the driver's own session should be counted"
    assert by_email["driver@example.com"][3] == 1, "their upload should be counted"
    assert by_email["driver@example.com"][4] == 1, "their laps should be counted"
    assert by_email["boss@example.com"][2] == 0


def test_a_non_admin_cannot_read_the_listing(world):
    _, error = world["driver"].try_call("SELECT * FROM admin_user_overview()")
    assert error and "not authorised" in error.lower()


def test_anon_cannot_read_the_listing(world):
    _, error = world["anon"].try_call("SELECT * FROM admin_user_overview()")
    assert error is not None


def test_a_non_admin_still_sees_only_their_own_row(world):
    """The admin SELECT policy is additional, not a replacement -- adding it
    must not have widened what an ordinary driver can read."""
    rows = world["driver"].call("SELECT id FROM users")
    assert [r[0] for r in rows] == [world["driver_user"]]


def test_the_listing_flags_an_unlinked_account(world):
    """An account with no external_auth_id authenticates and is invisible to
    every policy. The admin page is the only place that state is visible."""
    with world["conn"].cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, email_verified, created_at) "
            "VALUES ('orphan@example.com',TRUE,now())"
        )
    rows = world["boss"].call("SELECT email, is_linked FROM admin_user_overview()")
    assert ("orphan@example.com", False) in rows
    assert ("driver@example.com", True) in rows


# -------------------------------------------------------------- deletion


def test_a_non_admin_cannot_delete_anyone(world):
    _, error = world["driver"].try_call(
        "SELECT admin_delete_user(%s)", (world["bystander_user"],)
    )
    assert error and "not authorised" in error.lower()
    with world["conn"].cursor() as cur:
        cur.execute("SELECT count(*) FROM users WHERE id=%s", (world["bystander_user"],))
        assert cur.fetchone()[0] == 1, "a non-admin's failed delete still removed the user"


def test_an_admin_cannot_delete_themselves(world):
    """The one mistake with no in-app recovery."""
    _, error = world["boss"].try_call("SELECT admin_delete_user(%s)", (world["boss_user"],))
    assert error and "your own account" in error.lower()


def test_the_last_admin_cannot_be_removed(world):
    with world["conn"].cursor() as cur:
        cur.execute("UPDATE users SET is_admin=TRUE WHERE id=%s", (world["driver_user"],))
    # Two admins now: the driver can go.
    world["boss"].call("SELECT admin_delete_user(%s)", (world["driver_user"],))
    # One left, and it is the caller, so both guards apply.
    _, error = world["boss"].try_call("SELECT admin_delete_user(%s)", (world["boss_user"],))
    assert error is not None


def test_deleting_a_user_removes_their_sessions_and_everything_under_them(world):
    world["boss"].call("SELECT admin_delete_user(%s)", (world["driver_user"],))

    with world["conn"].cursor() as cur:
        for table, column in (
            ("users", "id"),
            ("driver_profiles", "user_id"),
        ):
            cur.execute(f"SELECT count(*) FROM {table} WHERE {column}=%s", (world["driver_user"],))
            assert cur.fetchone()[0] == 0, f"{table} row survived"

        cur.execute("SELECT count(*) FROM sessions WHERE id=%s", (world["session"],))
        assert cur.fetchone()[0] == 0
        for table in ("laps", "session_analysis", "lap_traces"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE session_db_id=%s", (world["session"],))
            assert cur.fetchone()[0] == 0, f"{table} was not cascaded"
        cur.execute("SELECT count(*) FROM upload_batches WHERE id=%s", (world["batch"],))
        assert cur.fetchone()[0] == 0


def test_deleting_a_user_removes_their_auth_identity(world):
    """Otherwise they sign in again, the signup trigger builds them a fresh
    empty account, and the delete looks like it silently failed."""
    result = world["boss"].call("SELECT admin_delete_user(%s)", (world["driver_user"],))[0][0]
    assert result["auth_identity_deleted"] is True
    assert result["sessions_deleted"] == 1

    with world["conn"].cursor() as cur:
        cur.execute("SELECT count(*) FROM auth.users WHERE id=%s", (world["driver_uid"],))
        assert cur.fetchone()[0] == 0


def test_deleting_a_user_does_not_take_other_peoples_data_with_them(world):
    """The collateral damage that matters: a team they founded, a profile
    they created for another driver, and that driver's sessions."""
    world["boss"].call("SELECT admin_delete_user(%s)", (world["driver_user"],))

    with world["conn"].cursor() as cur:
        cur.execute("SELECT created_by_user_id FROM teams WHERE id=%s", (world["team"],))
        row = cur.fetchone()
        assert row is not None, "a team was deleted along with the member who founded it"
        assert row[0] is None

        cur.execute("SELECT count(*) FROM driver_profiles WHERE id=%s", (world["guest_profile"],))
        assert cur.fetchone()[0] == 1, "a profile created for another driver was deleted"

        cur.execute("SELECT count(*) FROM sessions WHERE id=%s", (world["bystander_session"],))
        assert cur.fetchone()[0] == 1, "another driver's session was deleted"

        cur.execute("SELECT count(*) FROM users WHERE id=%s", (world["bystander_user"],))
        assert cur.fetchone()[0] == 1


def test_deleting_an_unknown_user_is_an_error_not_a_silent_success(world):
    _, error = world["boss"].try_call("SELECT admin_delete_user(%s)", (999999,))
    assert error is not None


def test_the_admin_flag_cannot_be_granted_from_the_app(world):
    """Privilege has to come from outside the application. `is_admin` is not
    in the column-level UPDATE grant, so no client can mint an admin -- which
    is what stops any flaw in the app from becoming a total compromise."""
    for actor in ("driver", "boss"):
        with pytest.raises(psycopg2.Error) as caught:
            world[actor].call(
                "UPDATE users SET is_admin=TRUE WHERE id=%s", (world["driver_user"],)
            )
        assert "permission denied" in str(caught.value).lower()
