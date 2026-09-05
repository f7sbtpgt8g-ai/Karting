"""Detects whether this Windows machine is currently connected to a
UniGo device's own WiFi access point, by parsing `netsh wlan show
interfaces` -- there's no cross-platform way to read the current SSID
from plain Python/stdlib, which is exactly why this lives in the
Windows-only platform layer rather than in `core/`.

Windows sometimes flags a no-internet WiFi network (which the device's
local-only AP always looks like) with a warning icon or "limited
connectivity" -- this only reads the SSID, so that's not a concern here;
it doesn't affect whether `core/device_client.py`'s direct HTTP calls to
the device's local IP work.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger("unigo_sync.platform_windows.wifi")

_SSID_RE = re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.MULTILINE)


def get_current_ssid() -> str | None:
    """Return the SSID of the currently-connected WiFi network, or None
    if not connected to WiFi at all (or `netsh` isn't available, e.g.
    when this is imported/tested on a non-Windows machine)."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("could not run netsh: %s", exc)
        return None

    if result.returncode != 0:
        logger.debug("netsh exited %d: %s", result.returncode, result.stderr.strip())
        return None

    match = _SSID_RE.search(result.stdout)
    return match.group(1) if match else None


def is_connected_to_unigo(ssid_prefix: str = "unigo-") -> bool:
    """True if the current WiFi SSID starts with `ssid_prefix` (default
    matches the device's own AP naming convention, confirmed in
    ../../findings.md -- update the prefix in config.yaml if a firmware
    update changes it)."""
    ssid = get_current_ssid()
    return ssid is not None and ssid.lower().startswith(ssid_prefix.lower())
