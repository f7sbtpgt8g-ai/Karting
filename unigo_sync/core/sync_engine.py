"""Orchestrates one sync pass: list what's on the device, figure out
what's new, download + convert + write each one, and record what
happened -- logged and tracked so a failure on one session doesn't lose
track of the others or get silently retried forever.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from .config import SyncConfig
from .device_client import DeviceClient, DeviceError
from .period import session_in_period
from .sync_state import SyncState
from .tsv_writer import write_tsv
from .uni_format import UniFormatError, decode_uni_bytes

logger = logging.getLogger("unigo_sync.sync_engine")

# Appended to the original .uni filename (minus extension) when writing
# the converted TSV, so it's visually distinct from a manually-exported
# Analyser TSV sitting in the same folder and never collides with one.
_OUTPUT_SUFFIX = ".unigo_sync.tsv"


@dataclass
class SyncResult:
    new_synced: list[str] = field(default_factory=list)
    already_synced: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (name, error)
    paths: dict[str, str] = field(default_factory=dict)  # session name -> written TSV path
    # Present on the device but outside the requested sync period -- never
    # downloaded, so this is cheap to report even when it's a large number.
    skipped_out_of_period: list[str] = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return len(self.new_synced) + len(self.already_synced) + len(self.failed)


def _output_path(output_dir: str, session_name: str) -> str:
    base = os.path.splitext(session_name)[0]
    # session names can contain spaces/non-ASCII (see findings.md) --
    # keep them, just strip characters that are awkward in filenames on
    # both Windows and the eventual iOS port.
    safe = "".join(c for c in base if c not in '<>:"/\\|?*')
    return os.path.join(output_dir, safe + _OUTPUT_SUFFIX)


def configure_logging(config: SyncConfig) -> None:
    parent = os.path.dirname(config.log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    root = logging.getLogger("unigo_sync")
    if root.handlers:
        return  # already configured (e.g. by a caller or a previous run in the same process)
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(config.log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console_handler)


def run_sync(
    config: SyncConfig,
    client: DeviceClient | None = None,
    state: SyncState | None = None,
    period_cutoff: datetime | None = None,
) -> SyncResult:
    """Run one full sync pass. Safe to call repeatedly (e.g. from a
    background watcher) -- already-synced sessions are skipped cheaply
    via `SyncState`, and a failure on one session is logged and recorded
    without aborting the rest of the run.

    `period_cutoff` (from `core.period.cutoff_for`) excludes sessions
    recorded before it *before* downloading anything -- see `core/period.py`
    for why filtering on the filename's embedded date is safe and why an
    unparseable name is kept rather than skipped. None means no filtering
    (sync everything the device has).
    """
    own_state = state is None
    client = client or DeviceClient(config)
    state = state or SyncState(config.sync_state_db)
    os.makedirs(config.output_dir, exist_ok=True)

    result = SyncResult()
    try:
        try:
            sessions = client.list_sessions()
        except DeviceError as exc:
            logger.error("could not list sessions from device: %s", exc)
            raise

        logger.info("device reports %d session(s)", len(sessions))

        for entry in sessions:
            name, size = entry["name"], entry["size"]
            if not session_in_period(name, period_cutoff):
                result.skipped_out_of_period.append(name)
                continue
            if state.is_synced(name, size):
                result.already_synced.append(name)
                continue

            logger.info("syncing new session: %s (%d bytes)", name, size)
            try:
                raw = client.download_session(name)
            except DeviceError as exc:
                logger.warning("download failed for %s: %s", name, exc)
                state.record_attempt(name, size, "failed", error=f"download failed: {exc}")
                result.failed.append((name, str(exc)))
                continue

            try:
                df = decode_uni_bytes(raw)
            except UniFormatError as exc:
                logger.warning("decode failed for %s: %s", name, exc)
                state.record_attempt(name, size, "failed", error=f"decode failed: {exc}")
                result.failed.append((name, str(exc)))
                continue

            out_path = _output_path(config.output_dir, name)
            try:
                write_tsv(df, out_path)
            except OSError as exc:
                logger.warning("write failed for %s: %s", name, exc)
                state.record_attempt(name, size, "failed", error=f"write failed: {exc}")
                result.failed.append((name, str(exc)))
                continue

            logger.info("synced %s -> %s (%d rows)", name, out_path, len(df))
            state.record_attempt(name, size, "success", local_path=out_path)
            result.new_synced.append(name)
            result.paths[name] = out_path

    finally:
        if own_state:
            state.close()

    logger.info(
        "sync pass complete: %d new, %d already synced, %d failed",
        len(result.new_synced),
        len(result.already_synced),
        len(result.failed),
    )
    return result
