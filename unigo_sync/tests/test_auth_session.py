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
