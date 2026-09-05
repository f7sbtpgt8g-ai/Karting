"""HTTP client for a UniGo device's local web server -- see
../findings.md's "Endpoints seen or referenced" table for how these were
found and confirmed.

Deliberately narrow: this client only knows how to do the two things a
sync tool needs (list sessions, download one), and never constructs a
URL from anything other than a name the device's own listing endpoint
gave us. `/file?delete=<name>` is a real, confirmed-destructive endpoint
on this device (a plain GET, not even a POST) that this client
deliberately never touches -- see the safety check in `download_session`.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import SyncConfig

logger = logging.getLogger("unigo_sync.device_client")


class DeviceError(Exception):
    """Base class for anything that went wrong talking to the device."""


class DeviceUnreachable(DeviceError):
    """The device didn't respond at all (not connected to its WiFi,
    dropped connection, timeout, etc.) -- expected to happen routinely
    at a track, not a bug."""


class DeviceResponseError(DeviceError):
    """The device responded, but not the way we expected (bad status,
    unparseable JSON, missing fields)."""


class UnsafeFilenameError(DeviceError):
    """A filename from the device's own listing endpoint looked
    suspicious enough that we refuse to build a URL from it -- see the
    module docstring on why `/file?delete=` makes this worth checking."""


def _looks_unsafe(name: str) -> bool:
    lowered = name.lower()
    return "delete=" in lowered or "update" in lowered or "\r" in name or "\n" in name


class DeviceClient:
    def __init__(self, config: SyncConfig):
        self.config = config
        self._session = requests.Session()

    def _get_with_retries(self, url: str, timeout: float) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self._session.get(url, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("request to %s failed (attempt %d/%d): %s", url, attempt, self.config.max_retries, exc)
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_backoff_s * attempt)
        raise DeviceUnreachable(f"could not reach {url} after {self.config.max_retries} attempts: {last_exc}") from last_exc

    def list_sessions(self) -> list[dict]:
        """Return every session the device currently has, as a list of
        {"name": str, "size": int}. No pagination on this device -- one
        call returns everything (see findings.md; can be hundreds of
        entries on a device that's never been cleared out)."""
        resp = self._get_with_retries(self.config.filelist_url, self.config.request_timeout_s)
        try:
            data = resp.json()
            files = data["files"]
            return [{"name": f["name"], "size": int(f["size"])} for f in files]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeviceResponseError(f"unexpected /file?filelist response shape: {exc}") from exc

    def download_session(self, name: str) -> bytes:
        """Download one session's raw bytes by name (as returned from
        `list_sessions`)."""
        if _looks_unsafe(name):
            raise UnsafeFilenameError(f"refusing to build a download URL from suspicious filename: {name!r}")
        url = self.config.download_url(name)
        resp = self._get_with_retries(url, self.config.download_timeout_s)
        return resp.content
