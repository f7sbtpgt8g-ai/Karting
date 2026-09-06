"""Sync tool configuration -- endpoints, timeouts, and local file paths,
loaded from a YAML file rather than hard-coded, so a firmware update that
changes an endpoint (see ../findings.md's "Recommended path" notes) is a
one-line config edit, not a code change.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import yaml


def _default_config_path() -> str:
    """Where to look for config.yaml when no path is given.

    In a normal source checkout this is next to the package (../config.yaml
    relative to this file). In a PyInstaller-frozen build, __file__ points
    into the onefile bundle's ephemeral extraction directory rather than
    anywhere the installed app's config.yaml actually lives -- the
    installer places config.yaml next to the .exe instead, so look there
    (`sys.executable`'s directory) when frozen. See `sys.frozen`, the
    standard PyInstaller marker for this.
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "config.yaml")
    return os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def _default_data_root() -> str:
    """What the relative paths in config.yaml (`output_dir`, `sessions_db`,
    ...) are resolved against.

    Deliberately *not* the process working directory. Anchoring on the cwd
    meant the same install read a different `sessions_db` depending on
    where it happened to be launched from -- and since
    `AccountLibrary.__init__` creates an empty SQLite file with a fresh
    schema rather than failing on a missing one, the visible symptom was
    "Incorrect email or password" against a database that had simply never
    had any accounts in it. Anchoring instead means the config's default
    `data/sessions.db` names one file per install, whatever the shortcut's
    working directory says.

    Frozen: the directory holding the .exe, i.e. the same place the
    installer puts config.yaml. Source checkout: the repo root, which is
    what `app.py`'s own `DB_PATH` resolves to, so running the GUI from a
    checkout shares the Streamlit app's database as the config implies.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@dataclass
class SyncConfig:
    # Device connection. Confirmed against firmware 1.20.002 -- see
    # findings.md's "Device info" / "Endpoints seen or referenced" table.
    # Re-run the discovery harness (../discovery/) and update these if a
    # firmware update changes them.
    base_url: str = "http://192.168.4.1"
    filelist_path: str = "/file?filelist"
    download_path_template: str = "/file?filename={name}"
    request_timeout_s: float = 15.0
    download_timeout_s: float = 60.0
    max_retries: int = 3
    retry_backoff_s: float = 2.0

    # Local state.
    output_dir: str = "data/unigo_sync/incoming"
    sync_state_db: str = "data/unigo_sync/sync_state.db"
    log_path: str = "data/unigo_sync/sync.log"

    # Analysis session library this tool ingests into (same file
    # `scripts/ingest.py --db` and the Streamlit app default to) when no
    # Postgres/Supabase database is configured via SUPABASE_DB_URL /
    # DATABASE_URL -- see telemetry.db.has_postgres_configured.
    sessions_db: str = "data/sessions.db"

    # Cached login (email/session token/chosen driver) for the GUI, so a
    # sign-in made while there's internet survives into an offline sync
    # pass at the track -- see core/auth_cache.py.
    auth_cache_path: str = "data/unigo_sync/auth_cache.json"

    # Sessions downloaded and decoded while offline (or while the
    # sessions database was otherwise unreachable) but not yet uploaded
    # into sessions_db -- see core/pending_uploads.py.
    pending_uploads_db: str = "data/unigo_sync/pending_uploads.db"

    # Background watcher (optional, off by default -- manual "sync now"
    # is the default trigger per the original design).
    poll_interval_s: float = 30.0

    # WiFi SSID prefix the Windows platform layer looks for before
    # syncing -- devices name their AP "unigo-xxxx".
    wifi_ssid_prefix: str = "unigo-"

    extra: dict = field(default_factory=dict)

    @property
    def filelist_url(self) -> str:
        return self.base_url.rstrip("/") + self.filelist_path

    def download_url(self, name: str) -> str:
        from urllib.parse import quote

        return self.base_url.rstrip("/") + self.download_path_template.format(name=quote(name))


# telemetry.auth/accounts/storage's own *_from_env factories all key off
# these environment variables (see telemetry/db.py's has_postgres_configured
# and telemetry/auth.py's provider_from_env) to decide between the local
# SQLite backend and a Supabase/Postgres one. That convention makes sense
# for a server deployment where env vars are part of the platform config,
# but there's no comparable place to set one on an end user's Windows
# laptop -- so config.yaml is also allowed to carry them (as plain,
# lowercase keys, since they land in `extra`) and `load_config` mirrors
# whichever of them is present into the process environment, letting the
# installed config.yaml be the actual place someone points a laptop at a
# real deployment's database, not a manual Windows env-var edit.
_ENV_CONFIG_KEYS = ("supabase_url", "supabase_anon_key", "supabase_db_url", "database_url")

# Config fields naming a local file or directory. Any of these left
# relative is resolved against `_default_data_root()` on load -- see there
# for why the process working directory is the wrong anchor.
_PATH_KEYS = (
    "output_dir", "sync_state_db", "log_path", "sessions_db",
    "auth_cache_path", "pending_uploads_db",
)


def load_config(path: str | None = None, data_root: str | None = None) -> SyncConfig:
    """Load config from YAML, falling back to defaults for anything not
    present in the file (including a missing file entirely).

    Relative local paths in the file are made absolute against `data_root`
    (default `_default_data_root()`), so every consumer of the returned
    config sees the same files regardless of the working directory it was
    launched from.
    """
    path = path or _default_config_path()
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    known = {k for k in SyncConfig.__dataclass_fields__ if k != "extra"}
    kwargs = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}

    for key in _ENV_CONFIG_KEYS:
        value = extra.get(key)
        # An already-set env var (e.g. a real server deployment) always
        # wins over config.yaml, so this never overrides a value the
        # deployment deliberately set another way.
        if value and not os.environ.get(key.upper()):
            os.environ[key.upper()] = str(value)

    config = SyncConfig(extra=extra, **kwargs)
    return resolve_data_paths(config, data_root or _default_data_root())


def resolve_data_paths(config: SyncConfig, data_root: str) -> SyncConfig:
    """Make every relative local path in `config` absolute against
    `data_root`, in place. An already-absolute path is left alone, so a
    config.yaml that spells out a full path (e.g. pointing a laptop at a
    checkout's `data/sessions.db`) still wins."""
    for key in _PATH_KEYS:
        value = getattr(config, key)
        if value and not os.path.isabs(value):
            setattr(config, key, os.path.normpath(os.path.join(data_root, value)))
    return config
