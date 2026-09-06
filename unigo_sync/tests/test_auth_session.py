"""End-to-end tests for core/auth_session.py against the local SQLite
auth/accounts backend (the same one `tests/test_auth.py` exercises) --
covers the actual "check credentials against DB, then pick a driver"
flow the GUI's login and settings screens are built on."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from telemetry.accounts import AccountLibrary  # noqa: E402
from telemetry.auth import AuthStore, LocalAuthProvider  # noqa: E402
from telemetry.storage import SessionLibrary  # noqa: E402
from unigo_sync.core import auth_session  # noqa: E402


@pytest.fixture(autouse=True)
def _no_postgres_env(monkeypatch):
    """These tests exercise the local SQLite backend specifically --
    make sure a Postgres/Supabase env var from the host environment can't
    silently redirect `provider_from_env`/`account_library_from_env`."""
    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_DB_URL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def db_path(tmp_path):
    path = os.path.join(tmp_path, "sessions.db")
    SessionLibrary(path)  # creates the sessions table driver_profiles-related FKs reference
    return path


def _register(db_path: str, email: str, password: str, display_name: str) -> int:
    accounts = AccountLibrary(db_path)
    store = AuthStore(db_path)
    result = LocalAuthProvider(accounts, store).register(email, password, display_name=display_name)
    assert result.ok, result.error
    return result.user_id


def test_login_succeeds_and_issues_a_session_token(db_path):
    _register(db_path, "austin@example.com", "correct horse battery", "Austin")

    result = auth_session.login("austin@example.com", "correct horse battery", db_path)

    assert result.ok is True
    assert result.user_id is not None
    assert result.session_token


def test_login_fails_with_wrong_password(db_path):
    _register(db_path, "austin@example.com", "correct horse battery", "Austin")

    result = auth_session.login("austin@example.com", "wrong password", db_path)

    assert result.ok is False
    assert result.session_token is None
    # A database that *does* have accounts keeps the deliberately vague
    # message -- distinguishing "no such email" from "wrong password" there
    # would be an account-enumeration hole.
    assert result.error == "Incorrect email or password."


def test_login_against_an_empty_database_reports_a_configuration_problem(tmp_path):
    """The database file is created on demand with a fresh schema, so a
    misconfigured `sessions_db` (or a laptop never pointed at the shared
    platform) produces a valid but empty database rather than an error.
    Reporting that as a bad password sends people off resetting a password
    that was never wrong."""
    path = os.path.join(tmp_path, "sessions.db")

    result = auth_session.login("austin@example.com", "correct horse battery", path)

    assert result.ok is False
    assert "No accounts exist" in result.error
    assert os.path.abspath(path) in result.error
    assert "supabase_url" in result.error
    # And it must not claim the credentials themselves were rejected.
    assert "Incorrect email or password" not in result.error


def test_empty_database_check_is_skipped_entirely_when_postgres_is_configured(monkeypatch):
    """`telemetry.db.connect` has no connect timeout by default, so probing
    Postgres on a failed sign-in would hang for minutes on exactly the path
    most likely to have no route to it -- a laptop joined to the device's
    own offline AP. The check must not even ask."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example/db")

    class ExplodingAccounts:
        def count_users(self):
            raise AssertionError("must not query the database")

    assert auth_session._local_database_is_empty(ExplodingAccounts()) is False


def test_describe_backend_names_the_local_file_when_supabase_is_not_configured(db_path):
    assert os.path.abspath(db_path) in auth_session.describe_backend(db_path)


def test_describe_backend_names_the_platform_when_supabase_is_configured(db_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://example/db")

    described = auth_session.describe_backend(db_path)

    assert "shared UniGo platform" in described
    assert os.path.abspath(db_path) not in described


def test_validate_session_round_trips_and_sign_out_revokes_it(db_path):
    _register(db_path, "austin@example.com", "correct horse battery", "Austin")
    login_result = auth_session.login("austin@example.com", "correct horse battery", db_path)

    assert auth_session.validate_session(login_result.session_token, db_path) == login_result.user_id

    auth_session.sign_out(login_result.session_token, db_path)

    assert auth_session.validate_session(login_result.session_token, db_path) is None


def test_driver_choices_include_own_profile_first(db_path):
    user_id = _register(db_path, "austin@example.com", "correct horse battery", "Austin")

    choices = auth_session.list_driver_choices(user_id, db_path)

    assert choices[0].display_name == "Austin"
    assert choices[0].is_own is True


def test_create_driver_adds_an_unclaimed_profile_visible_to_everyone(db_path):
    user_id = _register(db_path, "manager@example.com", "correct horse battery", "Manager")

    new_driver = auth_session.create_driver("Teammate Jamie", user_id, db_path)
    choices = auth_session.list_driver_choices(user_id, db_path)

    assert new_driver.is_own is False
    assert any(c.profile_id == new_driver.profile_id and c.display_name == "Teammate Jamie" for c in choices)


def test_driver_choices_do_not_duplicate_across_two_users(db_path):
    user_a = _register(db_path, "a@example.com", "correct horse battery", "Driver A")
    _register(db_path, "b@example.com", "correct horse battery", "Driver B")

    choices_for_a = auth_session.list_driver_choices(user_a, db_path)
    own = [c for c in choices_for_a if c.is_own]

    assert len(own) == 1
    assert own[0].display_name == "Driver A"
