"""Tests for core/pending_uploads.py -- the local queue of sessions
downloaded/decoded but not yet uploaded, used while the sessions database
is unreachable (e.g. connected to the UniGo device's own WiFi AP)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core.pending_uploads import PendingUploadQueue  # noqa: E402


def test_add_and_list_pending():
    with tempfile.TemporaryDirectory() as tmp:
        with PendingUploadQueue(str(Path(tmp) / "pending.db")) as queue:
            queue.add("260829_1441_Barmosen_AUSTIN.uni", "/staging/a.tsv", 7, "Austin", "Barmosen", "practice")
            pending = queue.list_pending()
            assert len(pending) == 1
            assert pending[0].name == "260829_1441_Barmosen_AUSTIN.uni"
            assert pending[0].driver_profile_id == 7
            assert pending[0].driver_display_name == "Austin"
            assert pending[0].attempts == 0


def test_remove_clears_entry():
    with tempfile.TemporaryDirectory() as tmp:
        with PendingUploadQueue(str(Path(tmp) / "pending.db")) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
            queue.remove("a.uni")
            assert queue.list_pending() == []
            assert queue.count() == 0


def test_record_attempt_failed_increments_attempts_and_sets_error():
    with tempfile.TemporaryDirectory() as tmp:
        with PendingUploadQueue(str(Path(tmp) / "pending.db")) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
            queue.record_attempt_failed("a.uni", "database unreachable")
            [record] = queue.list_pending()
            assert record.attempts == 1
            assert record.last_error == "database unreachable"


def test_add_same_name_twice_replaces_rather_than_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        with PendingUploadQueue(str(Path(tmp) / "pending.db")) as queue:
            queue.add("a.uni", "/staging/a.tsv", 1, "Austin")
            queue.record_attempt_failed("a.uni", "boom")
            queue.add("a.uni", "/staging/a.tsv", 1, "Austin")  # re-queued after a fresh sync pass
            [record] = queue.list_pending()
            assert record.attempts == 0
            assert record.last_error is None


def test_persists_across_reopening_the_same_file():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "pending.db")
        with PendingUploadQueue(db_path) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
        with PendingUploadQueue(db_path) as queue:
            assert queue.count() == 1
