"""Tests for core/sync_state.py -- the already-synced tracker."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core.sync_state import SyncState  # noqa: E402


def test_not_synced_initially():
    with tempfile.TemporaryDirectory() as tmp:
        with SyncState(str(Path(tmp) / "state.db")) as state:
            assert state.is_synced("a.uni", 1234) is False


def test_synced_after_success():
    with tempfile.TemporaryDirectory() as tmp:
        with SyncState(str(Path(tmp) / "state.db")) as state:
            state.record_attempt("a.uni", 1234, "success", local_path="/tmp/a.tsv")
            assert state.is_synced("a.uni", 1234) is True


def test_size_mismatch_is_treated_as_not_synced():
    """If the device somehow reports a different size for the same
    name, treat it as new rather than silently skipping it."""
    with tempfile.TemporaryDirectory() as tmp:
        with SyncState(str(Path(tmp) / "state.db")) as state:
            state.record_attempt("a.uni", 1234, "success")
            assert state.is_synced("a.uni", 5678) is False


def test_failed_attempt_not_marked_synced():
    with tempfile.TemporaryDirectory() as tmp:
        with SyncState(str(Path(tmp) / "state.db")) as state:
            state.record_attempt("a.uni", 1234, "failed", error="device unreachable")
            assert state.is_synced("a.uni", 1234) is False
            failed = state.list_failed()
            assert len(failed) == 1
            assert failed[0].error == "device unreachable"


def test_attempts_increments_on_repeated_calls():
    with tempfile.TemporaryDirectory() as tmp:
        with SyncState(str(Path(tmp) / "state.db")) as state:
            state.record_attempt("a.uni", 1234, "failed", error="timeout")
            state.record_attempt("a.uni", 1234, "failed", error="timeout")
            state.record_attempt("a.uni", 1234, "success")
            records = state.list_all()
            assert len(records) == 1
            assert records[0].attempts == 3
            assert records[0].status == "success"


def test_persists_across_reopen():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "state.db")
        with SyncState(db_path) as state:
            state.record_attempt("a.uni", 1234, "success")
        with SyncState(db_path) as state:
            assert state.is_synced("a.uni", 1234) is True
