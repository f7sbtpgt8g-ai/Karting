"""What the Row Level Security policies actually permit and deny.

These policies have shipped since the Supabase migration but have never
gated a real request: the Streamlit app connects on a superuser connection
that bypasses RLS entirely, and no account has ever had an
`external_auth_id`, so `current_app_user_id()` has always returned NULL.
Part 2 of the Next.js migration points a browser client at PostgREST under
the `authenticated` role, at which point these policies become the only
thing standing between one driver and another driver's data.

So this suite treats them as a security boundary and tests them as one:
every assertion runs as the `authenticated` role with a real JWT claim set,
against the migrations applied to a real Postgres, with Supabase's own
default grants in place (see supabase/testing/simulate_supabase.sql -- the
grants matter, because RLS only ever restricts what a GRANT already allows,
so a table with RLS *disabled* and grants present is wide open).

Requires a local Postgres. Skipped when one isn't reachable, so the normal
suite still runs anywhere:
    RLS_TEST_DSN=postgresql://postgres:postgres@localhost:5432/rls_test
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

ADMIN_DSN = os.environ.get("RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
TEST_DB = os.environ.get("RLS_TEST_DB", "rls_test")


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
    result = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"applying {os.path.basename(path)} failed:\n{result.stderr}")


@pytest.fixture(scope="module")
def db():
    """A fresh database with the simulation + every migration applied, in
    filename order -- the same order `supabase db push` would use, so a
    later migration correcting an earlier one is exercised as deployed."""
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

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn
    conn.close()


class Actor:
    """One authenticated client, as PostgREST would present it: the
    `authenticated` role plus a JWT whose `sub` is this user's Supabase Auth
    id. Every query runs in its own transaction so the role/claim reset
    cleanly, exactly like a per-request connection from the pooler."""

    def __init__(self, conn, auth_uid: str | None):
        self.conn = conn
        self.auth_uid = auth_uid

    def try_query(self, sql: str, params: tuple = ()) -> tuple[list[tuple], str | None]:
        """Like `query`, but returns `(rows, error)` instead of raising.

        A table can deny a client in two different ways, and both count as
        denied: RLS filters every row away (empty result), or the grant
        itself is missing (`permission denied`, raised). The second is
        stronger -- the client cannot even learn the table exists -- so a
        test for "this must not be readable" has to accept either.
        """
        try:
            return (self.query(sql, params), None)
        except psycopg2.Error as exc:
            return ([], str(exc).splitlines()[0])

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                claims = f'{{"sub":"{self.auth_uid}","role":"authenticated"}}' if self.auth_uid else '{"role":"anon"}'
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute(f"SET LOCAL ROLE {'authenticated' if self.auth_uid else 'anon'}")
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else []
            finally:
                cur.execute("ROLLBACK")

    def write(self, sql: str, params: tuple = ()) -> tuple[bool, str | None]:
        """Attempt a write. Returns (allowed, error). A row-count of 0 on an
        UPDATE/DELETE counts as denied -- RLS filters those silently rather
        than raising, which is the failure mode most likely to be mistaken
        for 'it worked'."""
        with self.conn.cursor() as cur:
            cur.execute("BEGIN")
            try:
                claims = f'{{"sub":"{self.auth_uid}","role":"authenticated"}}' if self.auth_uid else '{"role":"anon"}'
                cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
                cur.execute(f"SET LOCAL ROLE {'authenticated' if self.auth_uid else 'anon'}")
                cur.execute(sql, params)
                return (cur.rowcount > 0, None)
            except psycopg2.Error as exc:
                return (False, str(exc).splitlines()[0])
            finally:
                cur.execute("ROLLBACK")


@pytest.fixture(scope="module")
def world(db):
    """Two drivers on the same team, one driver on no team, and one session
    each at every visibility tier -- the smallest world that can tell the
    three tiers apart."""
    alice_uid, bob_uid, carol_uid = (str(uuid.uuid4()) for _ in range(3))
    with db.cursor() as cur:
        ids = {}
        for name, uid in (("alice", alice_uid), ("bob", bob_uid), ("carol", carol_uid)):
            cur.execute(
                "INSERT INTO users (email, external_auth_id, email_verified, display_name, created_at) "
                "VALUES (%s,%s,TRUE,%s,now()) RETURNING id",
                (f"{name}@example.com", uid, name.title()),
            )
            ids[f"{name}_user"] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at) "
                "VALUES (%s,%s,'claimed',now(),now()) RETURNING id",
                (name.title(), ids[f"{name}_user"]),
            )
            ids[f"{name}_profile"] = cur.fetchone()[0]

        # Alice + Bob share a team; Carol is unaffiliated.
        cur.execute(
            "INSERT INTO teams (name, created_by_user_id, created_at) VALUES ('Reds',%s,now()) RETURNING id",
            (ids["alice_user"],),
        )
        ids["team"] = cur.fetchone()[0]
        for who, role in (("alice", "manager"), ("bob", "member")):
            cur.execute(
                "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at, decided_at) "
                "VALUES (%s,%s,%s,'active',now(),now())",
                (ids["team"], ids[f"{who}_profile"], role),
            )

        # One Alice session per visibility tier.
        for tier in ("private", "team", "shared"):
            cur.execute(
                "INSERT INTO sessions (source_file, session_index, driver, track_name, start_date, "
                "ingested_at, best_lap_s, n_laps, driver_profile_id, uploaded_by_user_id, visibility, "
                "attribution_status) VALUES (%s,0,'Alice','Ring','2026-01-01',now(),30.0,10,%s,%s,%s,'confirmed') "
                "RETURNING id",
                (f"alice_{tier}.tsv", ids["alice_profile"], ids["alice_user"], tier),
            )
            ids[f"session_{tier}"] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO laps (session_db_id, lap_number, lap_time_s, is_outlier) VALUES (%s,1,30.0,FALSE)",
                (ids[f"session_{tier}"],),
            )
            cur.execute(
                "INSERT INTO session_cache (session_db_id, dataframe_parquet) VALUES (%s, %s)",
                (ids[f"session_{tier}"], psycopg2.Binary(b"not-really-parquet")),
            )

        # A password-reset token and an outbound email for Alice -- the kind
        # of row that must never be readable by another account.
        cur.execute(
            "INSERT INTO auth_tokens (user_id, kind, token, expires_at, created_at) "
            "VALUES (%s,'password_reset','SECRET-RESET-TOKEN', now() + interval '1 hour', now())",
            (ids["alice_user"],),
        )
        cur.execute(
            "INSERT INTO auth_sessions (user_id, token, created_at, expires_at) "
            "VALUES (%s,'SECRET-SESSION-TOKEN', now(), now() + interval '30 days')",
            (ids["alice_user"],),
        )
        cur.execute(
            "INSERT INTO email_outbox (to_email, subject, body, kind, created_at) "
            "VALUES ('alice@example.com','Reset your password','link: /reset?token=SECRET-RESET-TOKEN',"
            "'password_reset', now())"
        )
    db.commit()

    return {
        **ids,
        "alice": Actor(db, alice_uid),
        "bob": Actor(db, bob_uid),
        "carol": Actor(db, carol_uid),
        "anon": Actor(db, None),
    }


# ----------------------------------------------------------- session reads


def test_owner_sees_all_own_sessions_regardless_of_visibility(world):
    rows = world["alice"].query("SELECT id FROM sessions ORDER BY id")
    assert len(rows) == 3, "owner should see their own private, team and shared sessions"


def test_teammate_sees_team_and_shared_but_not_private(world):
    visible = {r[0] for r in world["bob"].query("SELECT id FROM sessions")}
    assert world["session_team"] in visible
    assert world["session_shared"] in visible
    assert world["session_private"] not in visible


def test_outsider_sees_only_shared(world):
    visible = {r[0] for r in world["carol"].query("SELECT id FROM sessions")}
    assert visible == {world["session_shared"]}


def test_anonymous_sees_no_sessions(world):
    assert world["anon"].query("SELECT id FROM sessions") == []


def test_child_tables_inherit_session_visibility(world):
    """laps / session_cache policies defer to `sessions`' own policy via a
    subquery. That only holds if RLS applies inside the subquery too -- if
    it didn't, the raw telemetry blob of a private session would leak."""
    for table, col in (("laps", "session_db_id"), ("session_cache", "session_db_id")):
        seen = {r[0] for r in world["carol"].query(f"SELECT {col} FROM {table}")}
        assert world["session_private"] not in seen, f"{table} leaked a private session"
        assert world["session_team"] not in seen, f"{table} leaked a team-only session to an outsider"
        assert seen == {world["session_shared"]}


# --------------------------------------------------------- the secret tables


@pytest.mark.parametrize(
    "table,secret",
    [
        ("auth_tokens", "SECRET-RESET-TOKEN"),
        ("auth_sessions", "SECRET-SESSION-TOKEN"),
        ("email_outbox", "SECRET-RESET-TOKEN"),
    ],
)
def test_other_users_cannot_read_auth_secrets(world, table, secret):
    """Any authenticated user reading another account's password-reset or
    session token is straightforward account takeover.

    Before 0002 these three tables had no RLS at all, and Supabase's default
    grants made them readable by every authenticated user -- this test failed
    against 0001 alone, which is why 0002 exists.
    """
    rows, error = world["carol"].try_query(f"SELECT * FROM {table}")
    dumped = " ".join(str(v) for row in rows for v in row)
    assert secret not in dumped, f"{table} exposed {secret} to an unrelated authenticated user"
    # The grant is revoked outright, so denial should be at privilege level.
    assert error is not None and "permission denied" in error, (
        f"{table} should be unreachable by a client entirely, got rows={rows!r} error={error!r}"
    )


def test_users_table_does_not_leak_other_accounts(world):
    rows = world["carol"].query("SELECT email FROM users")
    assert {r[0] for r in rows} <= {"carol@example.com"}


# ---------------------------------------------------------------- writes


def test_a_driver_can_change_their_own_session_visibility(world):
    """The app's single most common write. If this is denied, 'My Sessions
    & Sharing' cannot work against RLS at all."""
    allowed, err = world["alice"].write(
        "UPDATE sessions SET visibility='private' WHERE id=%s", (world["session_shared"],)
    )
    assert allowed, f"owner could not update their own session: {err}"


def test_a_driver_cannot_change_someone_elses_session(world):
    allowed, _ = world["carol"].write(
        "UPDATE sessions SET visibility='shared' WHERE id=%s", (world["session_private"],)
    )
    assert not allowed, "an unrelated user modified another driver's session"


def test_a_driver_cannot_delete_someone_elses_session(world):
    allowed, _ = world["carol"].write("DELETE FROM sessions WHERE id=%s", (world["session_shared"],))
    assert not allowed, "an unrelated user deleted another driver's session"


def test_a_driver_can_request_to_join_a_team(world):
    """Carol asking to join the Reds -- the join flow's only client write."""
    allowed, err = world["carol"].write(
        "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
        "VALUES (%s,%s,'member','pending',now())",
        (world["team"], world["carol_profile"]),
    )
    assert allowed, f"a driver could not request to join a team: {err}"


def test_a_driver_cannot_grant_themselves_active_membership(world):
    """The join request must not be self-approvable -- otherwise anyone can
    walk into any team and read its members' team-visible telemetry."""
    allowed, _ = world["carol"].write(
        "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at, decided_at) "
        "VALUES (%s,%s,'manager','active',now(),now())",
        (world["team"], world["carol_profile"]),
    )
    assert not allowed, "a driver granted themselves active team membership"


# ------------------------------------------------------- the upload queue
#
# `POST /api/uploads/confirm` inserts an `upload_batches` row as the caller,
# under RLS -- there is no service-role key in the Next.js app. So these
# policies, not the route handler, are what actually stop one driver from
# enqueueing work as another, or from marking their own unparsed file done.


def test_a_driver_can_enqueue_their_own_upload(world):
    allowed, err = world["alice"].write(
        "INSERT INTO upload_batches (storage_path, original_filename, uploaded_by_user_id, "
        "driver_profile_id, track_name, visibility, status) "
        "VALUES ('uid/one.tsv','one.tsv',%s,%s,'Ring','shared','pending')",
        (world["alice_user"], world["alice_profile"]),
    )
    assert allowed, f"a driver could not queue their own upload: {err}"


def test_a_driver_cannot_enqueue_an_upload_as_someone_else(world):
    """Otherwise Carol could file sessions into Alice's library."""
    allowed, _ = world["carol"].write(
        "INSERT INTO upload_batches (storage_path, uploaded_by_user_id, status) "
        "VALUES ('uid/two.tsv',%s,'pending')",
        (world["alice_user"],),
    )
    assert not allowed, "a driver queued an upload owned by another account"


@pytest.mark.parametrize("status", ["complete", "processing", "failed"])
def test_a_client_can_only_ever_enqueue_pending_work(world, status):
    """'complete' would mark an unparsed file done and hide it from the
    worker forever; 'processing' would do the same by stalling the queue."""
    allowed, _ = world["alice"].write(
        "INSERT INTO upload_batches (storage_path, uploaded_by_user_id, status) "
        "VALUES ('uid/three.tsv',%s,%s)",
        (world["alice_user"], status),
    )
    assert not allowed, f"a client inserted a batch already in status {status!r}"


def test_a_driver_sees_only_their_own_upload_batches(world, db):
    """The upload page lists 'recent uploads' with no owner filter of its
    own -- it relies entirely on this policy."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO upload_batches (storage_path, original_filename, uploaded_by_user_id, status) "
            "VALUES ('alice-uid/secret.tsv','secret.tsv',%s,'complete') RETURNING id",
            (world["alice_user"],),
        )
        alice_batch = cur.fetchone()[0]
    db.commit()

    assert world["alice"].query("SELECT id FROM upload_batches WHERE id=%s", (alice_batch,))
    assert not world["carol"].query(
        "SELECT id FROM upload_batches WHERE id=%s", (alice_batch,)
    ), "a driver could see another driver's uploads"
    assert not world["anon"].query("SELECT id FROM upload_batches"), "uploads visible to anon"


def test_a_client_cannot_rewrite_a_batchs_outcome(world, db):
    """There is no UPDATE policy at all: only the worker, on the
    service-role connection, moves a batch through its states."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO upload_batches (storage_path, uploaded_by_user_id, status) "
            "VALUES ('alice-uid/own.tsv',%s,'pending') RETURNING id",
            (world["alice_user"],),
        )
        batch = cur.fetchone()[0]
    db.commit()

    allowed, _ = world["alice"].write(
        "UPDATE upload_batches SET status='complete', sessions_created=99 WHERE id=%s", (batch,)
    )
    assert not allowed, "a client rewrote the status of its own upload batch"


# ------------------------------------------------------------ the Home page
#
# Home reads sessions scoped to your own driver profile plus, for a team
# manager/admin, the team roster's -- and writes back session type, track
# name, conditions and visibility inline. Those writes go straight from the
# browser to PostgREST under the caller's own JWT, so these policies are the
# only thing between one driver and another driver's session rows.


def test_a_driver_can_edit_their_own_sessions_details(world):
    """The inline edits Home offers: session type, track name, conditions."""
    allowed, err = world["alice"].write(
        "UPDATE sessions SET session_type='Qualifying', track_name='Ring B', "
        "track_condition='Wet' WHERE id=%s",
        (world["session_shared"],),
    )
    assert allowed, f"a driver could not edit their own session: {err}"


def test_a_driver_cannot_edit_another_drivers_session_details(world):
    """Carol can *see* Alice's shared session on Leaderboards, so "visible"
    must not imply "editable"."""
    allowed, _ = world["carol"].write(
        "UPDATE sessions SET track_name='Hijacked' WHERE id=%s", (world["session_shared"],)
    )
    assert not allowed, "a driver edited a session belonging to someone else"


def test_a_teammate_cannot_edit_a_team_visible_session(world):
    """Bob sees Alice's team session, and being on her team is not authority
    over her data."""
    allowed, _ = world["bob"].write(
        "UPDATE sessions SET session_type='Final' WHERE id=%s", (world["session_team"],)
    )
    assert not allowed, "a teammate edited another member's session"


def test_a_manager_can_read_their_teams_roster(world):
    """Home widens a manager's scope to the whole roster, which needs this
    read to resolve the profile ids in the first place."""
    rows = world["alice"].query(
        "SELECT driver_profile_id FROM team_memberships WHERE team_id=%s AND status='active'",
        (world["team"],),
    )
    assert {r[0] for r in rows} == {world["alice_profile"], world["bob_profile"]}


def test_an_outsider_cannot_read_a_teams_roster(world):
    """Otherwise the roster is a membership list anyone can enumerate."""
    rows = world["carol"].query(
        "SELECT driver_profile_id FROM team_memberships WHERE team_id=%s", (world["team"],)
    )
    assert rows == [], "a non-member read a team's roster"


def test_driver_names_resolve_for_sessions_home_can_see(world):
    """Home groups by driver and shows the name, via an embedded join onto
    driver_profiles. If that policy denied the row the group headers would
    silently read 'Unknown driver'."""
    rows = world["bob"].query(
        "SELECT p.display_name FROM sessions s JOIN driver_profiles p "
        "ON p.id = s.driver_profile_id WHERE s.id=%s",
        (world["session_team"],),
    )
    assert rows == [("Alice",)]


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN GAP: an uploader can attribute a session to another driver's "
    "claimed profile AND mark it 'confirmed' in one PostgREST call, bypassing "
    "the confirmation step accounts.attribute_session() enforces. Not reachable "
    "from any screen built so far, but RLS -- not the UI -- is the boundary. "
    "Fixing it needs a BEFORE UPDATE trigger (WITH CHECK cannot see the old "
    "row), and touches the worker's attribution too, so it is deliberately not "
    "patched under a Home-page change. Flip this to a plain assert when fixed.",
)
def test_a_driver_cannot_attribute_a_session_to_someone_elses_profile(world):
    """`attribute_session(requires_confirmation=True)` exists because filing a
    session under another registered driver puts it in their history and on
    their leaderboard entry -- every such query filters on
    `attribution_status = 'confirmed'`. That consent step lives in Python, and
    Python is no longer on the path now that the browser writes to `sessions`
    directly."""
    allowed, _ = world["alice"].write(
        "UPDATE sessions SET driver_profile_id=%s, attribution_status='confirmed' WHERE id=%s",
        (world["carol_profile"], world["session_shared"]),
    )
    assert not allowed, "a driver attributed their session to another driver as confirmed"


# ------------------------------------------------- the stored analysis (0005)
#
# Traces, sector times and session analysis are what Lap Analysis reads, and
# they exist so the raw blob can be cleared. They must inherit their
# session's visibility exactly -- and must not be writable by a client, or
# anyone could forge the numbers a coaching page is built from.


@pytest.fixture(scope="module")
def analysis_rows(db, world):
    with db.cursor() as cur:
        for tier in ("private", "team", "shared"):
            session_id = world[f"session_{tier}"]
            cur.execute(
                "INSERT INTO session_analysis (session_db_id, best_lap, theoretical_best_s) "
                "VALUES (%s, 1, 30.0) ON CONFLICT (session_db_id) DO NOTHING",
                (session_id,),
            )
            cur.execute(
                "INSERT INTO lap_segment_times "
                "(session_db_id, lap_number, segment_index, segment_label, segment_kind, time_s) "
                "VALUES (%s,1,0,'Corner 1','corner',5.0) ON CONFLICT DO NOTHING",
                (session_id,),
            )
            cur.execute(
                "INSERT INTO lap_traces (session_db_id, lap_number, sample_count, "
                "distance_m, lap_time_s) VALUES (%s,1,2,'{0,10}','{0,1}') "
                "ON CONFLICT DO NOTHING",
                (session_id,),
            )
    db.commit()
    return world


@pytest.mark.parametrize("table", ["session_analysis", "lap_segment_times", "lap_traces"])
def test_stored_analysis_inherits_session_visibility(analysis_rows, table):
    world = analysis_rows
    owner = {r[0] for r in world["alice"].query(f"SELECT session_db_id FROM {table}")}
    teammate = {r[0] for r in world["bob"].query(f"SELECT session_db_id FROM {table}")}
    outsider = {r[0] for r in world["carol"].query(f"SELECT session_db_id FROM {table}")}

    assert owner == {world["session_private"], world["session_team"], world["session_shared"]}
    assert teammate == {world["session_team"], world["session_shared"]}, (
        f"{table} leaked a private session to a teammate"
    )
    assert outsider == {world["session_shared"]}, f"{table} leaked to an outsider"
    assert world["anon"].query(f"SELECT session_db_id FROM {table}") == []


@pytest.mark.parametrize("table", ["session_analysis", "lap_segment_times", "lap_traces"])
def test_a_client_cannot_write_stored_analysis(analysis_rows, table):
    """Only the worker writes these, on the service-role connection. A
    client that could edit them could put any lap time, sector or trace in
    front of a driver -- including on a leaderboard."""
    world = analysis_rows
    allowed, _ = world["alice"].write(
        f"UPDATE {table} SET session_db_id = session_db_id WHERE session_db_id = %s",
        (world["session_shared"],),
    )
    assert not allowed, f"a client updated {table} on their own session"

    allowed, _ = world["alice"].write(
        f"DELETE FROM {table} WHERE session_db_id = %s", (world["session_shared"],)
    )
    assert not allowed, f"a client deleted from {table}"


# --------------------------------------------------- manual lap exclusion (0006)
#
# Lap Analysis lets a driver exclude a lap they went off on. That write goes
# browser-to-PostgREST, and `laps.lap_time_s` -- which feeds every leaderboard
# in the app -- sits in the same row. So the interesting question is not
# whether the toggle works, it is what else the toggle's permission opens up.


def test_a_driver_can_exclude_a_lap_of_their_own_session(world):
    allowed, err = world["alice"].write(
        "UPDATE laps SET excluded_by_user = TRUE, exclusion_note = 'went off at 3' "
        "WHERE session_db_id = %s",
        (world["session_shared"],),
    )
    assert allowed, f"a driver could not exclude a lap of their own session: {err}"


def test_a_driver_cannot_exclude_laps_of_someone_elses_session(world):
    allowed, _ = world["carol"].write(
        "UPDATE laps SET excluded_by_user = TRUE WHERE session_db_id = %s",
        (world["session_shared"],),
    )
    assert not allowed, "a stranger excluded a lap from another driver's session"


def test_a_teammate_cannot_exclude_laps_they_can_merely_see(world):
    allowed, _ = world["bob"].write(
        "UPDATE laps SET excluded_by_user = TRUE WHERE session_db_id = %s",
        (world["session_team"],),
    )
    assert not allowed, "a teammate excluded a lap from another member's session"


@pytest.mark.parametrize(
    "column,value",
    [("lap_time_s", "12.345"), ("is_outlier", "FALSE"), ("lap_number", "99")],
)
def test_the_exclusion_grant_does_not_open_up_the_rest_of_the_row(world, column, value):
    """RLS decides which rows an UPDATE may touch, never which columns -- so
    the new policy is narrowed by a column-level GRANT instead. Without it,
    'let me mark this lap excluded' would also mean 'let me rewrite my lap
    time', on a table every leaderboard reads."""
    allowed, error = world["alice"].write(
        f"UPDATE laps SET {column} = {value} WHERE session_db_id = %s",
        (world["session_shared"],),
    )
    assert not allowed, f"a client rewrote laps.{column} on their own session"
    assert error and "permission denied" in error.lower(), (
        f"expected a column-level permission error, got: {error}"
    )
