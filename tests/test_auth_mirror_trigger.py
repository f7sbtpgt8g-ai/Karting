"""The database-side mirroring of Supabase Auth signups.

`supabase/migrations/0004_mirror_auth_users.sql` moves the "create the local
`users` row for this Supabase identity" step out of Python and into a trigger
on `auth.users`, because Python is no longer on the path: the Next.js app
signs up against GoTrue directly and holds only the anon key, and there is
deliberately no INSERT policy on `users`.

The failure this prevents is a silent one -- an account that authenticates
perfectly and is invisible to every RLS policy, because
`current_app_user_id()` resolves it through `users.external_auth_id` and
finds nothing. So these tests assert the link exists after signup, not just
that a row appeared.

Requires a local Postgres, same as tests/test_rls_policies.py:
    RLS_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import date, datetime, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")
psycopg2.extras = pytest.importorskip("psycopg2.extras")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")
SIMULATION = os.path.join(REPO, "supabase", "testing", "simulate_supabase.sql")

ADMIN_DSN = os.environ.get("RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
TEST_DB = os.environ.get("MIRROR_TEST_DB", "auth_mirror_test")


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
def db():
    """A fresh database per test -- these tests insert into `auth.users`,
    whose unique email constraint would otherwise carry across them."""
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


def _signup(conn, email: str, metadata: dict | None = None, confirmed: bool = False) -> str:
    """What GoTrue does on /signup: one row in `auth.users`."""
    auth_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth.users (id, email, raw_user_meta_data, email_confirmed_at) "
            "VALUES (%s, %s, %s, %s)",
            (
                auth_id,
                email,
                psycopg2.extras.Json(metadata) if metadata else None,
                datetime.now(timezone.utc) if confirmed else None,
            ),
        )
    return auth_id


def _user(conn, email: str) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
    return dict(row) if row else None


def test_a_supabase_signup_creates_a_linked_local_account(db):
    """Without the link, the account is invisible to every RLS policy."""
    auth_id = _signup(db, "new@example.com", {"display_name": "Newbie"})

    user = _user(db, "new@example.com")
    assert user is not None, "signing up created no local account at all"
    assert user["external_auth_id"] == auth_id, (
        "the local account exists but is not linked to the Supabase identity -- "
        "this is the silent failure mode: logged in, sees nothing"
    )
    assert user["display_name"] == "Newbie"


def test_current_app_user_id_resolves_the_new_account(db):
    """The end-to-end version of the above: the function every policy is
    written in terms of has to return this user's id."""
    auth_id = _signup(db, "policy@example.com")
    expected = _user(db, "policy@example.com")["id"]

    with db.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (f'{{"sub":"{auth_id}","role":"authenticated"}}',),
        )
        cur.execute("SET LOCAL ROLE authenticated")
        cur.execute("SELECT current_app_user_id()")
        resolved = cur.fetchone()[0]
        cur.execute("ROLLBACK")

    assert resolved == expected


def test_a_driver_profile_is_created_too(db):
    """An account with no profile cannot own a session, appear on a
    leaderboard, or join a team."""
    _signup(db, "profile@example.com", {"display_name": "Profiled"})
    user = _user(db, "profile@example.com")

    with db.cursor() as cur:
        cur.execute(
            "SELECT display_name, claim_status FROM driver_profiles WHERE user_id = %s",
            (user["id"],),
        )
        rows = cur.fetchall()
    assert rows == [("Profiled", "claimed")]


def test_display_name_falls_back_to_the_email(db):
    _signup(db, "noname@example.com")
    assert _user(db, "noname@example.com")["display_name"] == "noname@example.com"


def test_an_existing_local_account_is_adopted_not_duplicated(db):
    """A Streamlit-era account crossing over to Supabase Auth has to keep its
    id -- every session it owns references it."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, email_verified, display_name, created_at) "
            "VALUES ('old@example.com','hash',TRUE,'Veteran',now()) RETURNING id"
        )
        original_id = cur.fetchone()[0]

    auth_id = _signup(db, "old@example.com")

    user = _user(db, "old@example.com")
    assert user["id"] == original_id, "the existing account was duplicated instead of adopted"
    assert user["external_auth_id"] == auth_id
    assert user["display_name"] == "Veteran", "adopting overwrote the existing account's details"


def test_an_account_already_linked_elsewhere_is_refused(db):
    """Adopting it would hand one person's telemetry to another."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, external_auth_id, email_verified, created_at) "
            "VALUES ('taken@example.com','some-other-identity',TRUE,now())"
        )

    with pytest.raises(psycopg2.Error) as caught:
        _signup(db, "taken@example.com")
    assert "already linked" in str(caught.value)


# ------------------------------------------------------ guardian consent
#
# telemetry/accounts.py gates sharing for under-16s until a guardian has
# consented. That rule lived only in Python's `create_user()`; a signup that
# never touches Python must not skip it.


def test_a_minor_signup_lands_in_pending_consent(db):
    minor_dob = date.today().replace(year=date.today().year - 12).isoformat()
    _signup(
        db,
        "young@example.com",
        {"display_name": "Junior", "date_of_birth": minor_dob, "guardian_email": "parent@example.com"},
    )

    user = _user(db, "young@example.com")
    assert user["date_of_birth"] == minor_dob
    assert user["guardian_email"] == "parent@example.com"
    assert user["guardian_consent_status"] == "pending", (
        "an under-16 account was created as 'not_required' -- their telemetry "
        "would be shareable with no guardian having agreed"
    )


def test_an_adult_signup_needs_no_consent(db):
    adult_dob = date.today().replace(year=date.today().year - 30).isoformat()
    _signup(db, "grown@example.com", {"date_of_birth": adult_dob})
    assert _user(db, "grown@example.com")["guardian_consent_status"] == "not_required"


def test_an_unparseable_date_of_birth_does_not_break_signup(db):
    """`is_minor()` treats an unknown DOB as unknown rather than guessing;
    the trigger must not fail the whole signup over it either."""
    _signup(db, "typo@example.com", {"date_of_birth": "not-a-date"})
    user = _user(db, "typo@example.com")
    assert user is not None
    assert user["date_of_birth"] is None


# ------------------------------------------------------------- other paths


def test_confirming_an_email_marks_the_local_account_verified(db):
    auth_id = _signup(db, "confirm@example.com")
    assert _user(db, "confirm@example.com")["email_verified"] is False

    with db.cursor() as cur:
        cur.execute("UPDATE auth.users SET email_confirmed_at = now() WHERE id = %s", (auth_id,))

    assert _user(db, "confirm@example.com")["email_verified"] is True


def test_a_phone_only_signup_is_left_alone(db):
    """No email means no way to represent the account in this schema. Better
    to skip it than to invent a placeholder address."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO auth.users (id, email) VALUES (gen_random_uuid(), NULL)")
        cur.execute("SELECT count(*) FROM users")
        assert cur.fetchone()[0] == 0


def test_the_engine_class_is_carried_through_signup(db):
    """Registration asks for it, so the trigger has to read it -- otherwise
    the answer is collected, stored in the identity's metadata, and silently
    dropped."""
    _signup(db, "racer@example.com", {"display_name": "Racer", "engine_category": "Rotax Junior"})
    assert _user(db, "racer@example.com")["engine_category"] == "Rotax Junior"


def test_signing_up_without_a_class_is_fine(db):
    _signup(db, "undecided@example.com", {"display_name": "Undecided"})
    assert _user(db, "undecided@example.com")["engine_category"] is None


def test_adopting_an_account_does_not_overwrite_its_class(db):
    """A Streamlit-era account crossing over keeps whatever it already has:
    the driver set that deliberately, and the signup form's blank default
    must not wipe it."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, password_hash, email_verified, engine_category, created_at) "
            "VALUES ('veteran@example.com','hash',TRUE,'Rotax DD2',now())"
        )
    _signup(db, "veteran@example.com", {"engine_category": ""})
    assert _user(db, "veteran@example.com")["engine_category"] == "Rotax DD2"
