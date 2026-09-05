"""Caches a signed-in session (and the driver/sync settings chosen after
signing in) to a local JSON file, so a login made while there's normal
internet survives into a sync pass at the track where the laptop has
joined the UniGo device's own WiFi access point and therefore has no
route to the sessions database at all.

The token cached here is exactly the one `telemetry.auth.AuthStore` /
`SupabaseAuthStore` hand back from `start_session()` -- the same
server-side, revocable session Streamlit's own login gate uses (see
`app.py`'s `_set_session_token`). Caching it doesn't add a second,
weaker auth mechanism: it is the same 7-day session, just persisted to
disk instead of `st.session_state`, and it is re-validated with
`AuthStore.user_for_session` the moment the network is back (see
`core.connectivity`) rather than trusted forever.

Nothing in this file talks to the database or the network -- it is
deliberately just a plain read/write of a small JSON blob.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from telemetry.auth import LOGIN_SESSION_TTL_DAYS

from .period import DEFAULT_SYNC_PERIOD


@dataclass
class CachedSession:
    user_id: int
    email: str
    session_token: str
    cached_at: str  # ISO-8601, UTC
    driver_profile_id: int | None = None
    driver_display_name: str | None = None
    sync_period: str = DEFAULT_SYNC_PERIOD

    def is_stale(self, now: datetime | None = None) -> bool:
        """True once the *local* copy has outlived the session's own
        server-side TTL. This is a client-side mirror of that TTL only --
        the authoritative check is always `AuthStore.user_for_session`,
        run whenever the network allows it; this just avoids offering a
        cached login that's certainly already expired server-side."""
        now = now or datetime.now(timezone.utc)
        cached_at = datetime.fromisoformat(self.cached_at)
        return now - cached_at > timedelta(days=LOGIN_SESSION_TTL_DAYS)


def save(path: str, session: CachedSession) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(asdict(session), f)
    os.replace(tmp_path, path)  # atomic on both POSIX and Windows


def load(path: str) -> CachedSession | None:
    """The cached session, or None if there isn't one / it's unreadable
    (corrupt file, older/incompatible format) -- either way, the caller
    just falls back to showing the login screen."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return CachedSession(**data)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def clear(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def update_settings(path: str, *, driver_profile_id: int | None, driver_display_name: str | None, sync_period: str) -> None:
    """Persist a driver/period choice made in the settings screen against
    an already-cached session, so relaunching the app remembers the last
    selection instead of defaulting every time."""
    cached = load(path)
    if cached is None:
        return
    cached.driver_profile_id = driver_profile_id
    cached.driver_display_name = driver_display_name
    cached.sync_period = sync_period
    save(path, cached)
