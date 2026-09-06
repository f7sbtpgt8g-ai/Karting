"""Login and driver-selection logic shared by the GUI, kept separate from
any GUI toolkit so it's plain, testable Python.

Deliberately reuses the same `telemetry.auth` / `telemetry.accounts`
machinery the Streamlit app's own login gate uses (see `app.py`'s
`render_auth_gate`) rather than a second, parallel credential check --
"check credentials against DB" means this one database, whichever backend
(local SQLite or Supabase) `provider_from_env` selects for the current
deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from telemetry import db as pgdb
from telemetry.accounts import CLAIM_INVITED, CLAIM_UNCLAIMED, account_library_from_env
from telemetry.auth import auth_store_from_env, provider_from_env


@dataclass
class LoginResult:
    ok: bool
    user_id: int | None = None
    email: str | None = None
    session_token: str | None = None
    error: str | None = None


@dataclass
class DriverChoice:
    profile_id: int
    display_name: str
    is_own: bool


def describe_backend(sessions_db_path: str) -> str:
    """One short line naming the database this laptop will actually check
    credentials against, for the GUI to show under the sign-in form.

    Worth surfacing because the two backends fail identically from the
    user's side -- a laptop that was never pointed at the shared platform
    quietly checks an empty local file and reports bad credentials -- and
    because which one is active is decided by config.yaml rather than by
    anything visible in the app."""
    if pgdb.has_postgres_configured():
        return "Signing in against the shared UniGo platform database."
    return f"Signing in against the local file {os.path.abspath(sessions_db_path)}"


def _local_database_is_empty(accounts) -> bool:
    """Whether a *local* accounts database holds no login accounts at all.

    Restricted to the local backend on purpose. The empty-database case is
    specifically what an unconfigured laptop looks like, and asking the
    question over Postgres instead would open a connection with no timeout
    (`telemetry.db.connect`'s default) on the one code path most likely to
    be running with no route to it -- turning a failed sign-in at the track
    into a multi-minute hang.

    A database error is reported as False so the caller keeps the
    provider's own error rather than replacing it with a misleading one.
    """
    if pgdb.has_postgres_configured():
        return False
    try:
        return accounts.count_users() == 0
    except Exception:  # noqa: BLE001 - an unreadable DB is not an empty one
        return False


def _misconfigured_database_error(sessions_db_path: str) -> str:
    return (
        "No accounts exist in the database this app is set up to use:\n"
        f"    {os.path.abspath(sessions_db_path)}\n\n"
        "Your email and password were not wrong -- they were never checked against "
        "anything. This laptop has not been pointed at the shared UniGo platform, so "
        "it fell back to a local database file, and that file is empty.\n\n"
        "Fix it by setting supabase_url, supabase_anon_key and supabase_db_url in "
        "config.yaml (all three), next to UniGoSync.exe, then restarting this app."
    )


def login(email: str, password: str, sessions_db_path: str) -> LoginResult:
    """Check credentials against the configured database and, on
    success, mint a server-side session token (same 7-day session
    `AuthStore.start_session` issues for the Streamlit app -- see
    `core/auth_cache.py` for why that's what gets cached for offline
    use)."""
    accounts = account_library_from_env(sessions_db_path)
    store = auth_store_from_env(sessions_db_path)
    provider = provider_from_env(accounts, store)

    result = provider.login(email.strip(), password)
    if not result.ok:
        # A failure against a database with no accounts in it is a
        # configuration problem, not a credential one, and saying
        # "incorrect password" there sends people off resetting a password
        # that was never the issue. Safe to distinguish: "this database is
        # empty" reveals nothing about whether any particular address is
        # registered, which is what the vague message exists to protect.
        if _local_database_is_empty(accounts):
            return LoginResult(False, error=_misconfigured_database_error(sessions_db_path))
        return LoginResult(False, error=result.error or "Incorrect email or password.")

    token = store.start_session(result.user_id)
    return LoginResult(True, user_id=result.user_id, email=email.strip(), session_token=token)


def validate_session(session_token: str, sessions_db_path: str) -> int | None:
    """The user id behind a cached session token, or None if it's
    missing/expired/revoked. Only meaningful when the database is
    reachable -- callers on an offline sync pass should skip this check
    entirely and trust the cache, per `core/auth_cache.py`."""
    store = auth_store_from_env(sessions_db_path)
    return store.user_for_session(session_token)


def sign_out(session_token: str, sessions_db_path: str) -> None:
    store = auth_store_from_env(sessions_db_path)
    store.revoke_session(session_token)


def list_driver_choices(user_id: int, sessions_db_path: str) -> list[DriverChoice]:
    """Every driver profile this signed-in user could reasonably attribute
    a sync to: their own claimed profile first, then every unclaimed/
    invited profile in the system -- the same pool `app.py`'s "attribute
    this upload" screen offers under its "Someone not on the platform yet"
    option, since a shared laptop syncing several karts' loggers is
    exactly the "team manager uploading a shared logger's file" case
    `telemetry/accounts.py`'s module docstring describes. Registered
    *other* drivers are deliberately excluded here: attributing to them
    needs their confirmation (`attribute_session`/`requires_confirmation`
    in app.py), a multi-step flow this at-the-track tool doesn't try to
    reproduce -- do that from the web app instead.
    """
    accounts = account_library_from_env(sessions_db_path)
    choices: list[DriverChoice] = []

    own = accounts.get_profile_for_user(user_id)
    if own is not None:
        choices.append(DriverChoice(int(own["id"]), own["display_name"], is_own=True))

    unclaimed = accounts.list_profiles(claim_status=CLAIM_UNCLAIMED)
    invited = accounts.list_profiles(claim_status=CLAIM_INVITED)
    for df in (unclaimed, invited):
        for _, row in df.iterrows():
            choices.append(DriverChoice(int(row["id"]), row["display_name"], is_own=False))

    return choices


def create_driver(display_name: str, user_id: int, sessions_db_path: str) -> DriverChoice:
    """Add a new unclaimed driver profile (e.g. a teammate whose sessions
    are being synced from this laptop for the first time) and return it
    ready to select."""
    accounts = account_library_from_env(sessions_db_path)
    profile_id, _claim_token = accounts.create_unclaimed_profile(
        display_name.strip(), created_by_user_id=user_id,
    )
    return DriverChoice(profile_id, display_name.strip(), is_own=False)
