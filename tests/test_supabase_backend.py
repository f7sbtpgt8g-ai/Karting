"""Factory-selection tests for the Postgres/Supabase-backed data layer.

Doesn't require a reachable Postgres database: `has_postgres_configured`
only inspects the environment, and the Postgres-backed classes deliberately
don't connect in `__init__` (see e.g. `SupabaseSessionLibrary`'s
docstring), so constructing them with a bogus connection string is safe
here. A real round-trip against Postgres was verified manually against a
live local instance while building this (see the README's "Migrating the
database layer to Supabase" section) -- that's not repeated here since
this suite otherwise has no Postgres dependency.
"""

from __future__ import annotations

from telemetry import db as pgdb
from telemetry.accounts import AccountLibrary, SupabaseAccountLibrary, account_library_from_env
from telemetry.auth import AuthStore, SupabaseAuthStore, auth_store_from_env
from telemetry.mailer import (
    OutboxEmailSender,
    SupabaseOutboxEmailSender,
    SupabaseSmtpEmailSender,
    sender_from_env,
)
from telemetry.storage import SessionLibrary, SupabaseSessionLibrary, session_library_from_env


def test_no_postgres_configured_falls_back_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = str(tmp_path / "sessions.db")

    assert pgdb.has_postgres_configured() is False
    assert isinstance(session_library_from_env(db_path), SessionLibrary)
    assert isinstance(account_library_from_env(db_path), AccountLibrary)
    assert isinstance(auth_store_from_env(db_path), AuthStore)
    assert isinstance(sender_from_env(db_path), OutboxEmailSender)


def test_supabase_db_url_selects_postgres_backend(tmp_path, monkeypatch):
    # A syntactically valid but unreachable connection string -- fine here
    # since none of these constructors connect eagerly.
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pass@localhost:59999/nonexistent")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    db_path = str(tmp_path / "sessions.db")

    assert pgdb.has_postgres_configured() is True
    assert isinstance(session_library_from_env(db_path), SupabaseSessionLibrary)
    assert isinstance(account_library_from_env(db_path), SupabaseAccountLibrary)
    assert isinstance(auth_store_from_env(db_path), SupabaseAuthStore)
    assert isinstance(sender_from_env(db_path), SupabaseOutboxEmailSender)


def test_database_url_also_selects_postgres_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:59999/nonexistent")

    assert pgdb.has_postgres_configured() is True
    assert isinstance(session_library_from_env(str(tmp_path / "sessions.db")), SupabaseSessionLibrary)


def test_smtp_host_with_postgres_selects_supabase_smtp_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:pass@localhost:59999/nonexistent")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")

    assert isinstance(sender_from_env(str(tmp_path / "sessions.db")), SupabaseSmtpEmailSender)


def test_database_url_missing_raises_with_actionable_message(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        pgdb.database_url()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "SUPABASE_DB_URL" in str(exc)


def test_is_not_distinct_from_sql():
    assert pgdb.is_not_distinct_from_sql("start_time") == "start_time IS NOT DISTINCT FROM %s"
