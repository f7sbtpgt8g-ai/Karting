"""Tests for core/sync_orchestrator.py -- the decision of whether a
newly-synced session gets uploaded immediately or queued for later,
and the background flush that drains the queue once the database is
reachable again."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import unigo_sync.tests.uni_fixtures as fx  # noqa: E402
from unigo_sync.core import sync_orchestrator  # noqa: E402
from unigo_sync.core.config import SyncConfig  # noqa: E402
from unigo_sync.core.connectivity import Reachability  # noqa: E402
from unigo_sync.core.pending_uploads import PendingUploadQueue  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_blocked_reason_reporting():
    """`_report_blocked` only logs on a change, via module-level state --
    reset it so one test's reason can't suppress the next test's."""
    sync_orchestrator._last_blocked_reason = None
    yield
    sync_orchestrator._last_blocked_reason = None


def _patch_reachable(monkeypatch, ok: bool, reason: str | None = None) -> None:
    monkeypatch.setattr(sync_orchestrator, "probe", lambda: Reachability(ok, reason))


def _valid_uni_bytes() -> bytes:
    return fx.build_uni_file(records=[fx.gps_fix_record(55.05, 11.91, 0.5, 10.0)])


class FakeClient:
    def __init__(self, sessions: dict[str, bytes]):
        self.sessions = sessions

    def list_sessions(self):
        return [{"name": name, "size": len(data)} for name, data in self.sessions.items()]

    def download_session(self, name):
        return self.sessions[name]


class FakeLibrary:
    """Stands in for SessionLibrary/SupabaseSessionLibrary -- just needs
    `.close()`; `ingest_one` itself is monkeypatched per test."""

    def close(self):
        pass


def _tmp_config(tmp: str) -> SyncConfig:
    return SyncConfig(
        output_dir=os.path.join(tmp, "out"),
        sync_state_db=os.path.join(tmp, "state.db"),
        log_path=os.path.join(tmp, "sync.log"),
        sessions_db=os.path.join(tmp, "sessions.db"),
        pending_uploads_db=os.path.join(tmp, "pending.db"),
    )


def test_new_session_is_uploaded_immediately_when_online(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        _patch_reachable(monkeypatch, True)
        monkeypatch.setattr(sync_orchestrator, "session_library_from_env", lambda path: FakeLibrary())

        ingested = []
        monkeypatch.setattr(
            sync_orchestrator, "ingest_one",
            lambda library, path, **kw: ingested.append((path, kw)) or 1,
        )

        outcome = sync_orchestrator.sync_and_upload(
            config, period_cutoff=None, driver_profile_id=7, driver_display_name="Austin", client=client,
        )

        assert outcome.uploaded == ["a.uni"]
        assert outcome.queued == []
        assert len(ingested) == 1
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            assert queue.count() == 0


def test_new_session_is_queued_when_offline(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        _patch_reachable(monkeypatch, False, "connection refused")
        # Uploading should never even be attempted while offline.
        monkeypatch.setattr(
            sync_orchestrator, "session_library_from_env",
            lambda path: pytest.fail("should not connect to the database while offline"),
        )

        outcome = sync_orchestrator.sync_and_upload(
            config, period_cutoff=None, driver_profile_id=7, driver_display_name="Austin", client=client,
        )

        assert outcome.uploaded == []
        assert outcome.queued == ["a.uni"]
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            [pending] = queue.list_pending()
            assert pending.driver_profile_id == 7
            assert pending.driver_display_name == "Austin"


def test_upload_failure_while_online_falls_back_to_queue(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        _patch_reachable(monkeypatch, True)
        monkeypatch.setattr(sync_orchestrator, "session_library_from_env", lambda path: FakeLibrary())
        monkeypatch.setattr(
            sync_orchestrator, "ingest_one",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db write failed")),
        )

        outcome = sync_orchestrator.sync_and_upload(
            config, period_cutoff=None, driver_profile_id=None, driver_display_name=None, client=client,
        )

        assert outcome.uploaded == []
        assert outcome.queued == ["a.uni"]
        assert outcome.upload_errors[0][0] == "a.uni"


def test_flush_pending_uploads_drains_queue_once_online(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        tsv_path = os.path.join(tmp, "staged.tsv")
        Path(tsv_path).write_text("fake tsv")
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", tsv_path, 3, "Austin")

        _patch_reachable(monkeypatch, True)
        monkeypatch.setattr(sync_orchestrator, "session_library_from_env", lambda path: FakeLibrary())
        monkeypatch.setattr(sync_orchestrator, "ingest_one", lambda *a, **kw: 1)

        outcome = sync_orchestrator.flush_pending_uploads(config)

        assert outcome.uploaded == ["a.uni"]
        assert outcome.still_pending == []
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            assert queue.count() == 0


def test_flush_pending_uploads_is_a_noop_while_offline(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)

        _patch_reachable(monkeypatch, False, "connection refused")

        outcome = sync_orchestrator.flush_pending_uploads(config)

        assert outcome.uploaded == []
        assert outcome.still_pending == ["a.uni"]
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            assert queue.count() == 1


def test_flush_pending_uploads_with_empty_queue_does_not_touch_database(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        monkeypatch.setattr(
            sync_orchestrator, "probe",
            lambda: pytest.fail("should not check connectivity for an empty queue"),
        )
        outcome = sync_orchestrator.flush_pending_uploads(config)
        assert outcome.uploaded == []
        assert outcome.still_pending == []


def test_offline_flush_reports_why_it_could_not_upload(monkeypatch):
    """The symptom this fixes: a queue that never drains and a queue that
    is merely waiting look identical from the GUI, because the reason was
    computed and thrown away."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
        _patch_reachable(monkeypatch, False, "could not translate host name")

        outcome = sync_orchestrator.flush_pending_uploads(config)

        assert outcome.offline_reason == "could not translate host name"
        assert outcome.blocked_reason == "database not reachable: could not translate host name"


def test_flush_reports_an_upload_error_when_the_database_is_reachable(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
        _patch_reachable(monkeypatch, True)
        monkeypatch.setattr(sync_orchestrator, "session_library_from_env", lambda path: FakeLibrary())
        monkeypatch.setattr(
            sync_orchestrator, "ingest_one",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no such driver profile")),
        )

        outcome = sync_orchestrator.flush_pending_uploads(config)

        assert outcome.still_pending == ["a.uni"]
        assert outcome.offline_reason is None
        assert outcome.blocked_reason == "upload failed: no such driver profile"
        # The item stays queued with the error recorded against it, so the
        # next attempt can report it too.
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            [pending] = queue.list_pending()
            assert pending.attempts == 1
            assert "no such driver profile" in pending.last_error


def test_a_clean_flush_has_no_blocked_reason(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
        _patch_reachable(monkeypatch, True)
        monkeypatch.setattr(sync_orchestrator, "session_library_from_env", lambda path: FakeLibrary())
        monkeypatch.setattr(sync_orchestrator, "ingest_one", lambda *a, **kw: 1)

        assert sync_orchestrator.flush_pending_uploads(config).blocked_reason is None


def test_sync_records_why_sessions_were_queued(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        _patch_reachable(monkeypatch, False, "timeout expired")

        outcome = sync_orchestrator.sync_and_upload(
            config, period_cutoff=None, driver_profile_id=None, driver_display_name=None, client=client,
        )

        assert outcome.queued == ["a.uni"]
        assert outcome.offline_reason == "timeout expired"


def test_unreachable_database_is_logged_once_per_change_not_once_per_poll(monkeypatch, caplog):
    """The flush runs every 15 seconds; a warning on every poll would bury
    the log file that is the only record of why uploads are stuck."""
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with PendingUploadQueue(config.pending_uploads_db) as queue:
            queue.add("a.uni", "/staging/a.tsv", None, None)
        _patch_reachable(monkeypatch, False, "connection refused")

        with caplog.at_level("WARNING", logger="unigo_sync.sync_orchestrator"):
            for _ in range(5):
                sync_orchestrator.flush_pending_uploads(config)

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "connection refused" in warnings[0].getMessage()
