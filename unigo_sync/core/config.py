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


def load_config(path: str | None = None) -> SyncConfig:
    """Load config from YAML, falling back to defaults for anything not
    present in the file (including a missing file entirely)."""
    path = path or _default_config_path()
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    known = {k for k in SyncConfig.__dataclass_fields__ if k != "extra"}
    kwargs = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return SyncConfig(extra=extra, **kwargs)
