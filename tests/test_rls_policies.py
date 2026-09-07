"""What the Row Level Security policies actually permit and deny.

These policies shipped before anything actually depended on them: the
worker connects on a superuser connection that bypasses RLS entirely, and
for a while no account had an `external_auth_id`, so
`current_app_user_id()` always returned NULL. `web/` now points a browser
client at PostgREST under the `authenticated` role, which makes these
policies the only thing standing between one driver and another driver's
data.

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
from contextlib import contextmanager

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


def _become(cur, actor: "Actor") -> None:
    """Switch an already-open transaction to act as `actor` for whatever
    statement runs next -- the multi-actor equivalent of `Actor.query`'s own
    per-call claim-setting, used by `scenario()` below to weave a sequence
    like "Dana requests, Alice approves, Dana can now read" through several
    identities inside one transaction, since `Actor.query`/`.write` each
    open and roll back their own transaction and so can never see an
    earlier call's effects."""
    claims = f'{{"sub":"{actor.auth_uid}","role":"authenticated"}}' if actor.auth_uid else '{"role":"anon"}'
    cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
    cur.execute(f"SET LOCAL ROLE {'authenticated' if actor.auth_uid else 'anon'}")


@contextmanager
def scenario(db):
    """One transaction, several actors, rolled back at the end regardless
    of outcome -- so a multi-step flow (request -> approve -> can-now-read)
    can be exercised as a single connected story without ever persisting
    anything past the test, matching every other test in this file."""
    with db.cursor() as cur:
        cur.execute("BEGIN")
        try:
            yield cur
        finally:
            cur.execute("ROLLBACK")


def _try(cur, sql: str, params: tuple = ()) -> tuple[bool, str | None, list[tuple] | None]:
    """Attempt one statement inside a `scenario`, without poisoning the rest
    of the transaction if it fails -- a raised exception aborts every
    following statement in the same Postgres transaction until rolled back,
    which a `scenario` test needs to survive to keep telling its story
    (e.g. "Bob's attempt to approve is refused, then Alice's succeeds").

    A plain UPDATE/DELETE that RLS filters down to zero matched rows does
    NOT raise -- it just quietly affects nothing, the same silent-denial
    trap `Actor.write` already guards against elsewhere in this file. So a
    statement with no result set (`cur.description is None`: an UPDATE/
    DELETE/INSERT with no RETURNING) additionally requires `rowcount > 0`
    to count as `ok`. A SELECT or an RPC call (`SELECT some_function(...)`,
    which always yields one row even for a VOID-returning function) always
    has a result set, so this never affects those -- callers checking "did
    I get the row(s) I expected" should still inspect `rows` themselves.
    """
    cur.execute("SAVEPOINT sp")
    try:
        cur.execute(sql, params)
        has_result_set = cur.description is not None
        rows = cur.fetchall() if has_result_set else None
        ok = True if has_result_set else cur.rowcount > 0
        cur.execute("RELEASE SAVEPOINT sp")
        return (ok, None, rows)
    except psycopg2.Error as exc:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
        return (False, str(exc).splitlines()[0], None)


def _insert_session(
    cur,
    *,
    driver_profile_id: int,
    uploaded_by_user_id: int,
    track_name: str,
    best_lap_s: float,
    average_lap_s: float | None = None,
    visibility: str = "shared",
    attribution_status: str = "confirmed",
    engine_category: str | None = None,
) -> int:
    """Insert one session for a track-leaderboard test. `source_file` is a
    fresh uuid every call so repeated inserts in the same test never collide
    (the fixture's own sessions use a per-tier name instead, since there is
    only ever one of each)."""
    cur.execute(
        "INSERT INTO sessions (source_file, session_index, driver, track_name, start_date, "
        "ingested_at, best_lap_s, average_lap_s, n_laps, driver_profile_id, uploaded_by_user_id, "
        "visibility, attribution_status, engine_category) "
        "VALUES (%s,0,'x',%s,'2026-01-01',now(),%s,%s,10,%s,%s,%s,%s,%s) RETURNING id",
        (
            f"{uuid.uuid4()}.tsv",
            track_name,
            best_lap_s,
            average_lap_s,
            driver_profile_id,
            uploaded_by_user_id,
            visibility,
            attribution_status,
            engine_category,
        ),
    )
    return cur.fetchone()[0]


def _insert_team(cur, name: str, created_by_user_id: int) -> int:
    cur.execute(
        "INSERT INTO teams (name, created_by_user_id, created_at) VALUES (%s,%s,now()) RETURNING id",
        (name, created_by_user_id),
    )
    return cur.fetchone()[0]


def _insert_claimed_driver(cur, display_name: str) -> tuple[int, int]:
    """A driver with no `Actor` of their own -- for tests that only need
    another team's roster to exist, not to query *as* that driver."""
    cur.execute(
        "INSERT INTO users (email, external_auth_id, email_verified, display_name, created_at) "
        "VALUES (%s,%s,TRUE,%s,now()) RETURNING id",
        (f"{display_name.lower()}-{uuid.uuid4().hex[:8]}@example.com", str(uuid.uuid4()), display_name),
    )
    user_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at) "
        "VALUES (%s,%s,'claimed',now(),now()) RETURNING id",
        (display_name, user_id),
    )
    return user_id, cur.fetchone()[0]


@pytest.fixture(scope="module")
def world(db):
    """Two drivers on the same team, one driver on no team, and one session
    each at every visibility tier -- the smallest world that can tell the
    three tiers apart."""
    alice_uid, bob_uid, carol_uid, dana_uid = (str(uuid.uuid4()) for _ in range(4))
    with db.cursor() as cur:
        ids = {}
        for name, uid in (("alice", alice_uid), ("bob", bob_uid), ("carol", carol_uid), ("dana", dana_uid)):
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
                "VALUES (%s,%s,%s,'active',now(),now()) RETURNING id",
                (ids["team"], ids[f"{who}_profile"], role),
            )
            ids[f"{who}_membership"] = cur.fetchone()[0]

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
        "dana": Actor(db, dana_uid),
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


# ------------------------------------------ a driver's own account (0007)


def test_a_driver_can_set_their_own_engine_class(world):
    allowed, err = world["alice"].write(
        "UPDATE users SET engine_category='Rotax Senior' WHERE id=%s", (world["alice_user"],)
    )
    assert allowed, f"a driver could not set their own engine class: {err}"


def test_a_driver_cannot_edit_another_account(world):
    allowed, _ = world["carol"].write(
        "UPDATE users SET engine_category='OK-N' WHERE id=%s", (world["alice_user"],)
    )
    assert not allowed, "a driver edited someone else's account row"


@pytest.mark.parametrize(
    "column,value",
    [
        ("email", "'hijack@example.com'"),
        ("external_auth_id", "'some-other-identity'"),
        ("guardian_consent_status", "'granted'"),
        ("email_verified", "TRUE"),
    ],
)
def test_the_settings_grant_does_not_open_up_the_account_row(world, column, value):
    """`users` holds the auth link and the guardian-consent state. An UPDATE
    policy chooses rows, never columns -- so without the column-level GRANT,
    "let me change my engine class" would also mean "let me grant my own
    guardian consent" and "let me repoint my account at another identity"."""
    allowed, error = world["alice"].write(
        f"UPDATE users SET {column} = {value} WHERE id=%s", (world["alice_user"],)
    )
    assert not allowed, f"a client rewrote users.{column} on their own account"
    assert error and "permission denied" in error.lower(), (
        f"expected a column-level permission error, got: {error}"
    )


# --------------------------------------------------------- team management
#
# Creating a team, requesting to join, approving/rejecting, promoting/
# demoting, removing a member, and transferring the manager role -- the five
# SECURITY DEFINER RPCs plus the tightened self-leave policy from
# 0010_teams_management.sql. Unlike the read-only team policies above, most
# of these depend on more than one row's state (the caller's own role in
# *this* team, the target's current role), so they're exercised as
# multi-step `scenario()` stories rather than single `Actor.write` calls.


def test_team_create_makes_caller_the_manager(world, db):
    with scenario(db) as cur:
        _become(cur, world["dana"])
        ok, err, rows = _try(cur, "SELECT team_create('Blues')")
        assert ok, f"team_create failed: {err}"
        team_id = rows[0][0]

        ok, err, rows = _try(
            cur, "SELECT role, status FROM team_memberships WHERE team_id=%s AND driver_profile_id=%s",
            (team_id, world["dana_profile"]),
        )
        assert ok and rows == [("manager", "active")], f"creator wasn't seated as active manager: {rows}"


def test_team_create_refuses_if_already_on_a_team(world, db):
    with scenario(db) as cur:
        _become(cur, world["bob"])  # already an active member of Reds
        ok, err, _ = _try(cur, "SELECT team_create('Yellows')")
        assert not ok, "a driver already on a team was allowed to create another"


def test_full_join_approve_flow_grants_team_visibility(world, db):
    with scenario(db) as cur:
        _become(cur, world["dana"])
        ok, err, rows = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
            "VALUES (%s,%s,'member','pending',now()) RETURNING id",
            (world["team"], world["dana_profile"]),
        )
        assert ok, f"Dana could not request to join: {err}"
        membership_id = rows[0][0]

        ok, err, rows = _try(cur, "SELECT id FROM sessions WHERE id=%s", (world["session_team"],))
        assert ok and rows == [], "a pending request already grants team-visibility access"

        _become(cur, world["bob"])  # plain member -- not authorised to decide
        ok, err, _ = _try(cur, "SELECT team_resolve_join_request(%s, TRUE)", (membership_id,))
        assert not ok, "a plain member was allowed to approve a join request"

        _become(cur, world["alice"])  # the manager
        ok, err, _ = _try(cur, "SELECT team_resolve_join_request(%s, TRUE)", (membership_id,))
        assert ok, f"the manager could not approve a pending request: {err}"

        _become(cur, world["dana"])
        ok, err, rows = _try(cur, "SELECT id FROM sessions WHERE id=%s", (world["session_team"],))
        assert ok and rows == [(world["session_team"],)], "approved teammate still can't see the team session"


def test_manager_can_reject_a_join_request(world, db):
    with scenario(db) as cur:
        _become(cur, world["dana"])
        _, _, rows = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
            "VALUES (%s,%s,'member','pending',now()) RETURNING id",
            (world["team"], world["dana_profile"]),
        )
        membership_id = rows[0][0]

        _become(cur, world["alice"])
        ok, err, _ = _try(cur, "SELECT team_resolve_join_request(%s, FALSE)", (membership_id,))
        assert ok, f"the manager could not reject a pending request: {err}"

        _become(cur, world["dana"])
        _, _, rows = _try(cur, "SELECT id FROM sessions WHERE id=%s", (world["session_team"],))
        assert rows == [], "a rejected request still grants team-visibility access"


def test_only_manager_can_promote_or_demote(world, db):
    """README rule: an admin may accept/reject requests and remove a plain
    member, but only the manager may change anyone's role."""
    with scenario(db) as cur:
        _become(cur, world["alice"])
        ok, err, _ = _try(cur, "SELECT team_set_member_role(%s, 'admin')", (world["bob_membership"],))
        assert ok, f"the manager could not promote a member to admin: {err}"

        _become(cur, world["bob"])  # now an admin
        ok, err, _ = _try(cur, "SELECT team_set_member_role(%s, 'admin')", (world["bob_membership"],))
        assert not ok, "an admin was allowed to change a role -- only the manager may"


def test_admin_can_resolve_requests_but_not_the_manager_role(world, db):
    with scenario(db) as cur:
        _become(cur, world["alice"])
        _try(cur, "SELECT team_set_member_role(%s, 'admin')", (world["bob_membership"],))

        _become(cur, world["dana"])
        _, _, rows = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
            "VALUES (%s,%s,'member','pending',now()) RETURNING id",
            (world["team"], world["dana_profile"]),
        )
        membership_id = rows[0][0]

        _become(cur, world["bob"])  # admin, not manager
        ok, err, _ = _try(cur, "SELECT team_resolve_join_request(%s, TRUE)", (membership_id,))
        assert ok, f"an admin could not approve a join request: {err}"

        ok, err, _ = _try(cur, "SELECT team_set_member_role(%s, 'admin')", (membership_id,))
        assert not ok, "an admin was allowed to promote the driver they just approved"


def test_admin_cannot_act_on_another_admin_only_manager_can(world, db):
    """README rule: an admin can remove a plain member, but only the
    manager may remove or demote a fellow admin -- admins can't act on
    each other. Alice (manager) admits Dana, then promotes both Bob and
    Dana to admin, so there are two admins to test against."""
    with scenario(db) as cur:
        _become(cur, world["dana"])
        _, _, rows = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
            "VALUES (%s,%s,'member','pending',now()) RETURNING id",
            (world["team"], world["dana_profile"]),
        )
        dana_membership_id = rows[0][0]

        _become(cur, world["alice"])
        _try(cur, "SELECT team_resolve_join_request(%s, TRUE)", (dana_membership_id,))
        _try(cur, "SELECT team_set_member_role(%s, 'admin')", (world["bob_membership"],))
        _try(cur, "SELECT team_set_member_role(%s, 'admin')", (dana_membership_id,))

        _become(cur, world["bob"])  # admin, targeting a fellow admin
        ok, err, _ = _try(cur, "SELECT team_remove_member(%s)", (dana_membership_id,))
        assert not ok, "an admin was allowed to remove another admin"

        _become(cur, world["alice"])  # manager, same target
        ok, err, _ = _try(cur, "SELECT team_remove_member(%s)", (dana_membership_id,))
        assert ok, f"the manager could not remove an admin: {err}"


def test_manager_cannot_leave_directly_but_can_after_transfer(world, db):
    with scenario(db) as cur:
        _become(cur, world["alice"])
        ok, err, _ = _try(
            cur, "UPDATE team_memberships SET status='left' WHERE driver_profile_id=%s", (world["alice_profile"],)
        )
        assert not ok, "the manager was allowed to leave directly, bypassing transfer"

        ok, err, _ = _try(cur, "SELECT team_transfer_manager(%s)", (world["bob_membership"],))
        assert ok, f"the manager could not transfer ownership: {err}"

        ok, err, rows = _try(
            cur, "SELECT role FROM team_memberships WHERE driver_profile_id=%s", (world["alice_profile"],)
        )
        assert ok and rows == [("admin",)], f"outgoing manager should land as admin, got: {rows}"

        # Now demoted to admin, Alice's own leave path is open again.
        ok, err, _ = _try(
            cur, "UPDATE team_memberships SET status='left' WHERE driver_profile_id=%s", (world["alice_profile"],)
        )
        assert ok, f"a demoted former manager could not leave: {err}"


def test_transfer_manager_requires_being_current_manager(world, db):
    with scenario(db) as cur:
        _become(cur, world["bob"])  # plain member
        ok, err, _ = _try(cur, "SELECT team_transfer_manager(%s)", (world["bob_membership"],))
        assert not ok, "a non-manager was allowed to transfer the manager role"


def test_partial_unique_index_blocks_second_live_membership_per_profile(world, db):
    """Defence in depth beyond team_create's own pre-check: even a direct
    INSERT (run as the connection's own owner role, bypassing RLS, to
    isolate the database-level backstop from the RLS layer that would
    normally prevent this insert shape anyway) can't put a second live row
    under one driver profile."""
    with scenario(db) as cur:
        cur.execute(
            "INSERT INTO teams (name, created_by_user_id, created_at) VALUES ('Greens',%s,now()) RETURNING id",
            (world["alice_user"],),
        )
        other_team_id = cur.fetchone()[0]

        # Bob is already an active member of Reds (from the fixture) -- a
        # second team's pending request for him should collide.
        ok, err, _ = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at) "
            "VALUES (%s,%s,'member','pending',now())",
            (other_team_id, world["bob_profile"]),
        )
        assert not ok, "a second live membership row was allowed for one driver profile"
        assert err and "duplicate key" in err.lower(), f"expected a unique-index violation, got: {err}"


def test_partial_unique_index_blocks_second_active_manager_per_team(world, db):
    """Bypasses RLS entirely (runs as the connection's own owner role, not
    as any Actor) to isolate the database-level backstop from the RLS layer
    that would normally prevent this insert shape anyway -- the point is
    that the constraint holds even if RLS or a future RPC didn't."""
    with scenario(db) as cur:
        ok, err, _ = _try(
            cur,
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at, decided_at) "
            "VALUES (%s,%s,'manager','active',now(),now())",
            (world["team"], world["dana_profile"]),
        )
        assert not ok, "a second active manager was allowed on one team"
        assert err and "duplicate key" in err.lower(), f"expected a unique-index violation, got: {err}"


@pytest.mark.parametrize("column,value", [("role", "'admin'"), ("driver_profile_id", "999999")])
def test_the_leave_grant_does_not_open_up_other_columns(world, column, value):
    """`team_memberships_leave_own`'s WITH CHECK pins status/driver_profile_id
    on the *resulting* row, but says nothing about which columns a self-leave
    PATCH may touch -- the column-level GRANT is what actually closes that,
    the same idiom `test_the_settings_grant_does_not_open_up_the_account_row`
    already covers for `users`."""
    allowed, error = world["bob"].write(
        f"UPDATE team_memberships SET status='left', {column}={value} WHERE driver_profile_id=%s",
        (world["bob_profile"],),
    )
    assert not allowed, f"a client rewrote team_memberships.{column} via their own leave request"
    assert error and "permission denied" in error.lower(), (
        f"expected a column-level permission error, got: {error}"
    )


# --------------------------------------------------------- track leaderboards
#
# There is no `tracks` table -- these RPCs aggregate over sessions.track_name
# directly (0011_track_leaderboards.sql). Most of them are plain SECURITY
# INVOKER reads that ride the sessions_select policy as-is. The two podium
# functions (track_driver_podium, track_team_podium) additionally repeat the
# public-visibility predicate themselves rather than trusting whatever RLS
# already narrowed things to, and track_team_podium runs as SECURITY DEFINER
# because team_memberships_select would otherwise collapse its cross-team
# ranking down to "the caller's own team, maybe". The tests below exist to
# prove exactly that -- that the podiums come out identical no matter who is
# asking, which is the one property a plain SECURITY INVOKER version would
# have silently gotten wrong.


def test_track_summaries_returns_only_rls_visible_tracks(world, db):
    """An outsider only sees a track's public+own sessions; a teammate
    additionally sees the team-visibility ones -- track_summaries rides RLS
    as-is (it isn't meant to be one canonical answer, unlike the podiums)."""
    with scenario(db) as cur:
        track = f"Summary Park {uuid.uuid4().hex[:6]}"
        _insert_session(
            cur, driver_profile_id=world["dana_profile"], uploaded_by_user_id=world["dana_user"],
            track_name=track, best_lap_s=50.0, visibility="shared",
        )
        _insert_session(
            cur, driver_profile_id=world["bob_profile"], uploaded_by_user_id=world["bob_user"],
            track_name=track, best_lap_s=40.0, visibility="team",
        )

        _become(cur, world["carol"])  # unaffiliated outsider
        cur.execute("SELECT session_count FROM track_summaries(NULL) WHERE track_name=%s", (track,))
        assert cur.fetchone() == (1,), "an outsider saw a team-only session in the track list"

        _become(cur, world["alice"])  # Bob's teammate
        cur.execute("SELECT session_count FROM track_summaries(NULL) WHERE track_name=%s", (track,))
        assert cur.fetchone() == (2,), "a teammate did not see the team-visibility session"


def test_track_my_best_is_scoped_to_the_caller_only(world, db):
    with scenario(db) as cur:
        track = f"Own Best Park {uuid.uuid4().hex[:6]}"
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=50.0,
        )
        _insert_session(
            cur, driver_profile_id=world["bob_profile"], uploaded_by_user_id=world["bob_user"],
            track_name=track, best_lap_s=40.0,
        )

        _become(cur, world["alice"])
        cur.execute("SELECT best_lap_s FROM track_my_best(%s, NULL)", (track,))
        assert cur.fetchone() == (50.0,), "Alice's own best leaked Bob's faster lap"

        _become(cur, world["bob"])
        cur.execute("SELECT best_lap_s FROM track_my_best(%s, NULL)", (track,))
        assert cur.fetchone() == (40.0,), "Bob's own best leaked Alice's lap instead of his own"


def test_track_driver_podium_and_team_podium_are_identical_for_every_caller(world, db):
    """The core correctness test. Reds (Alice) and a brand-new team Blues
    (Frank, who has no `Actor` and is never queried as -- only his team's
    existence matters) each have one public lap; Bob additionally has a
    *faster* team-only lap that must never appear anywhere. Three callers
    with three different relationships to these teams -- Carol (on neither),
    Bob (on Reds, not Blues), Alice (on Reds, not Blues) -- must all see the
    exact same podium, in the same order, including Blues' entry despite
    none of them being a Blues member."""
    with scenario(db) as cur:
        track = f"Podium Park {uuid.uuid4().hex[:6]}"
        blues_user, frank_profile = _insert_claimed_driver(cur, "Frank")
        blues_team = _insert_team(cur, "Blues", blues_user)
        cur.execute(
            "INSERT INTO team_memberships (team_id, driver_profile_id, role, status, requested_at, decided_at) "
            "VALUES (%s,%s,'manager','active',now(),now())",
            (blues_team, frank_profile),
        )

        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=50.0, visibility="shared",
        )
        _insert_session(
            cur, driver_profile_id=frank_profile, uploaded_by_user_id=blues_user,
            track_name=track, best_lap_s=45.0, visibility="shared",
        )
        _insert_session(
            cur, driver_profile_id=world["bob_profile"], uploaded_by_user_id=world["bob_user"],
            track_name=track, best_lap_s=10.0, visibility="team",  # never public -- must be excluded
        )

        results = {}
        for who in ("carol", "bob", "alice"):
            _become(cur, world[who])
            cur.execute(
                "SELECT driver_name, best_lap_s, team_name FROM track_driver_podium(%s, NULL, 10)", (track,)
            )
            drivers = cur.fetchall()
            cur.execute("SELECT team_name, best_lap_s FROM track_team_podium(%s, NULL, 3)", (track,))
            teams = cur.fetchall()
            results[who] = (drivers, teams)

        assert results["carol"] == results["bob"] == results["alice"], (
            f"the podium differed by caller: {results}"
        )
        drivers, teams = results["carol"]
        assert drivers == [("Frank", 45.0, "Blues"), ("Alice", 50.0, "Reds")], drivers
        assert teams == [("Blues", 45.0), ("Reds", 50.0)], teams
        assert 10.0 not in [d[1] for d in drivers], "Bob's team-only lap leaked into a public podium"


def test_track_podiums_exclude_private_and_unconfirmed_but_my_best_includes_them(world, db):
    with scenario(db) as cur:
        track = f"Exclusion Park {uuid.uuid4().hex[:6]}"
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=50.0, visibility="shared", attribution_status="confirmed",
        )
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=20.0, visibility="private", attribution_status="confirmed",
        )
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=10.0, visibility="shared", attribution_status="pending",
        )

        _become(cur, world["alice"])
        cur.execute("SELECT best_lap_s FROM track_my_best(%s, NULL)", (track,))
        assert cur.fetchone() == (10.0,), "Alice's own best did not pick up her fastest private/unconfirmed lap"

        for who in ("alice", "bob", "carol"):
            _become(cur, world[who])
            cur.execute("SELECT best_lap_s FROM track_driver_podium(%s, NULL, 10)", (track,))
            laps = [row[0] for row in cur.fetchall()]
            assert laps == [50.0], f"private/unconfirmed laps leaked into the public podium for {who}: {laps}"


def test_track_driver_podium_filters_by_engine_category(world, db):
    with scenario(db) as cur:
        track = f"Class Park {uuid.uuid4().hex[:6]}"
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=50.0, engine_category="Rotax Senior",
        )
        _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=45.0, engine_category="X30 Senior",
        )

        _become(cur, world["carol"])
        cur.execute(
            "SELECT best_lap_s FROM track_driver_podium(%s, 'Rotax Senior', 10)", (track,)
        )
        assert cur.fetchall() == [(50.0,)], "the class filter did not restrict to the requested class"

        cur.execute("SELECT best_lap_s, engine_category FROM track_driver_podium(%s, NULL, 10)", (track,))
        assert cur.fetchall() == [(45.0, "X30 Senior")], "unfiltered should surface the faster class"


def test_track_map_source_prefers_public_then_falls_back_to_own(world, db):
    with scenario(db) as cur:
        track = f"Map Park {uuid.uuid4().hex[:6]}"
        bob_session = _insert_session(
            cur, driver_profile_id=world["bob_profile"], uploaded_by_user_id=world["bob_user"],
            track_name=track, best_lap_s=40.0, visibility="private",
        )
        cur.execute("INSERT INTO session_analysis (session_db_id, best_lap) VALUES (%s, 1)", (bob_session,))

        _become(cur, world["bob"])
        cur.execute("SELECT session_id FROM track_map_source(%s, NULL)", (track,))
        assert cur.fetchone() == (bob_session,), "no public session yet -- should have fallen back to Bob's own"

        # sessions has no INSERT policy for `authenticated` at all (real
        # ingestion runs on the worker's superuser connection) -- drop back
        # to the connection's own unrestricted role before inserting again.
        cur.execute("RESET ROLE")

        # A slower public session should still win over Bob's faster private one.
        alice_session = _insert_session(
            cur, driver_profile_id=world["alice_profile"], uploaded_by_user_id=world["alice_user"],
            track_name=track, best_lap_s=60.0, visibility="shared",
        )
        cur.execute("INSERT INTO session_analysis (session_db_id, best_lap) VALUES (%s, 1)", (alice_session,))

        _become(cur, world["bob"])
        cur.execute("SELECT session_id FROM track_map_source(%s, NULL)", (track,))
        assert cur.fetchone() == (alice_session,), "a public session should be preferred even if slower"


def test_anonymous_gets_nothing_from_track_rpcs(world):
    for sql in (
        "SELECT * FROM track_summaries(NULL)",
        "SELECT * FROM track_driver_podium('Ring', NULL, 10)",
        "SELECT * FROM track_team_podium('Ring', NULL, 3)",
    ):
        rows, _ = world["anon"].try_query(sql)
        assert rows == [], f"anonymous got rows back from: {sql}"
