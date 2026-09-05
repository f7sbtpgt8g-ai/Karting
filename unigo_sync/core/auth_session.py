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

from dataclasses import dataclass

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
