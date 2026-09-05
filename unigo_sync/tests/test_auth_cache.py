"""Tests for core/auth_cache.py -- the local "remember me" cache that
lets a login made with internet survive into an offline sync pass."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core import auth_cache  # noqa: E402


def _session(cached_at: datetime | None = None) -> auth_cache.CachedSession:
    return auth_cache.CachedSession(
        user_id=1, email="driver@example.com", session_token="tok123",
        cached_at=(cached_at or datetime.now(timezone.utc)).isoformat(),
    )


def test_round_trip_save_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "nested" / "auth_cache.json")
        auth_cache.save(path, _session())
        loaded = auth_cache.load(path)
        assert loaded.user_id == 1
        assert loaded.email == "driver@example.com"
        assert loaded.session_token == "tok123"


def test_load_missing_file_returns_none():
    assert auth_cache.load("/nonexistent/auth_cache.json") is None


def test_load_corrupt_file_returns_none_not_raises():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "auth_cache.json")
        Path(path).write_text("{not valid json")
        assert auth_cache.load(path) is None


def test_clear_removes_file_and_is_safe_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "auth_cache.json")
        auth_cache.save(path, _session())
        auth_cache.clear(path)
        assert auth_cache.load(path) is None
        auth_cache.clear(path)  # no error on a second call


def test_is_stale_false_for_fresh_session():
    assert _session().is_stale() is False


def test_is_stale_true_past_the_login_session_ttl():
    old = _session(cached_at=datetime.now(timezone.utc) - timedelta(days=30))
    assert old.is_stale() is True


def test_update_settings_persists_driver_and_period():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "auth_cache.json")
        auth_cache.save(path, _session())
        auth_cache.update_settings(path, driver_profile_id=42, driver_display_name="Austin", sync_period="last_week")
        loaded = auth_cache.load(path)
        assert loaded.driver_profile_id == 42
        assert loaded.driver_display_name == "Austin"
        assert loaded.sync_period == "last_week"
        # The session itself is untouched.
        assert loaded.session_token == "tok123"


def test_update_settings_is_a_noop_with_no_cached_session():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "auth_cache.json")
        auth_cache.update_settings(path, driver_profile_id=1, driver_display_name="X", sync_period="today")
        assert auth_cache.load(path) is None
