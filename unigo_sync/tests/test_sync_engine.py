"""Tests for core/sync_engine.py -- the orchestrator, exercised with a
fake device client (no real device or network needed) built from
synthetic .uni fixtures."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import unigo_sync.tests.uni_fixtures as fx  # noqa: E402
from unigo_sync.core.config import SyncConfig  # noqa: E402
from unigo_sync.core.device_client import DeviceUnreachable  # noqa: E402
from unigo_sync.core.sync_engine import preview_sync, run_sync  # noqa: E402
from unigo_sync.core.sync_state import SyncState  # noqa: E402


def _valid_uni_bytes() -> bytes:
    return fx.build_uni_file(records=[fx.gps_fix_record(55.05, 11.91, 0.5, 10.0)])


class FakeClient:
    def __init__(self, sessions: dict[str, bytes]):
        self.sessions = sessions  # name -> raw bytes
        self.list_calls = 0
        self.download_calls = []

    def list_sessions(self):
        self.list_calls += 1
        return [{"name": name, "size": len(data)} for name, data in self.sessions.items()]

    def download_session(self, name):
        self.download_calls.append(name)
        return self.sessions[name]


class FailingListClient:
    def list_sessions(self):
        raise DeviceUnreachable("device not on WiFi")


def _tmp_config(tmp: str) -> SyncConfig:
    return SyncConfig(
        output_dir=os.path.join(tmp, "out"),
        sync_state_db=os.path.join(tmp, "state.db"),
        log_path=os.path.join(tmp, "sync.log"),
    )


def test_new_session_is_downloaded_converted_and_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        result = run_sync(config, client=client)

        assert result.new_synced == ["a.uni"]
        assert result.failed == []
        assert client.download_calls == ["a.uni"]
        assert os.path.exists(result.paths["a.uni"])


def test_already_synced_session_is_skipped_without_downloading():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        data = _valid_uni_bytes()
        client = FakeClient({"a.uni": data})

        run_sync(config, client=client)
        assert client.download_calls == ["a.uni"]

        result2 = run_sync(config, client=client)
        assert result2.new_synced == []
        assert result2.already_synced == ["a.uni"]
        # no second download -- only the first run touched the network
        assert client.download_calls == ["a.uni"]


def test_undecodable_session_is_recorded_as_failed_not_crashed():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"bad.uni": b"not a real uni file"})
        result = run_sync(config, client=client)

        assert result.new_synced == []
        assert len(result.failed) == 1
        assert result.failed[0][0] == "bad.uni"

        with SyncState(config.sync_state_db) as state:
            failed = state.list_failed()
            assert len(failed) == 1
            assert failed[0].name == "bad.uni"


def test_one_bad_session_does_not_block_others_in_the_same_pass():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"bad.uni": b"garbage", "good.uni": _valid_uni_bytes()})
        result = run_sync(config, client=client)

        assert result.new_synced == ["good.uni"]
        assert [name for name, _ in result.failed] == ["bad.uni"]


def test_device_unreachable_propagates_cleanly():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with pytest.raises(DeviceUnreachable):
            run_sync(config, client=FailingListClient())


def test_period_cutoff_skips_old_sessions_without_downloading():
    from datetime import datetime

    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({
            "260828_0900_Barmosen_AUSTIN.uni": _valid_uni_bytes(),  # before cutoff
            "260829_1000_Barmosen_AUSTIN.uni": _valid_uni_bytes(),  # on/after cutoff
        })
        result = run_sync(config, client=client, period_cutoff=datetime(2026, 8, 29))

        assert result.new_synced == ["260829_1000_Barmosen_AUSTIN.uni"]
        assert result.skipped_out_of_period == ["260828_0900_Barmosen_AUSTIN.uni"]
        # the out-of-period session was never even downloaded
        assert client.download_calls == ["260829_1000_Barmosen_AUSTIN.uni"]


def test_period_cutoff_none_syncs_everything():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes(), "b.uni": _valid_uni_bytes()})
        result = run_sync(config, client=client, period_cutoff=None)

        assert sorted(result.new_synced) == ["a.uni", "b.uni"]
        assert result.skipped_out_of_period == []


def test_out_of_period_session_is_not_recorded_in_sync_state():
    from datetime import datetime

    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"260101_0000_old.uni": _valid_uni_bytes()})
        run_sync(config, client=client, period_cutoff=datetime(2026, 8, 29))

        with SyncState(config.sync_state_db) as state:
            assert state.list_all() == []


def test_preview_sync_reports_counts_and_bytes_without_downloading():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        data_a, data_b = _valid_uni_bytes(), _valid_uni_bytes()
        client = FakeClient({"a.uni": data_a, "b.uni": data_b})

        preview = preview_sync(config, client=client)

        assert preview.total_on_device == 2
        assert preview.in_period == 2
        assert preview.already_synced == 0
        assert preview.new_count == 2
        assert preview.new_bytes == len(data_a) + len(data_b)
        # preview never downloads anything
        assert client.download_calls == []


def test_preview_sync_excludes_out_of_period_sessions():
    from datetime import datetime

    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({
            "260828_0900_old.uni": _valid_uni_bytes(),
            "260829_1000_new.uni": _valid_uni_bytes(),
        })

        preview = preview_sync(config, client=client, period_cutoff=datetime(2026, 8, 29))

        assert preview.total_on_device == 2
        assert preview.in_period == 1
        assert preview.new_count == 1


def test_preview_sync_counts_already_synced_sessions_separately():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        data = _valid_uni_bytes()
        client = FakeClient({"a.uni": data})
        run_sync(config, client=client)  # downloads and records it as synced

        preview = preview_sync(config, client=client)

        assert preview.new_count == 0
        assert preview.already_synced == 1
        assert preview.new_bytes == 0


def test_preview_sync_propagates_device_unreachable():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        with pytest.raises(DeviceUnreachable):
            preview_sync(config, client=FailingListClient())


def test_on_progress_called_once_per_in_period_session_with_running_total():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes(), "b.uni": _valid_uni_bytes()})
        calls = []

        run_sync(config, client=client, on_progress=lambda i, t, n: calls.append((i, t, n)))

        assert sorted(calls) == [(1, 2, "a.uni"), (2, 2, "b.uni")]


def test_on_progress_excludes_out_of_period_sessions():
    from datetime import datetime

    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({
            "260828_0900_old.uni": _valid_uni_bytes(),
            "260829_1000_new.uni": _valid_uni_bytes(),
        })
        calls = []

        run_sync(
            config, client=client, period_cutoff=datetime(2026, 8, 29),
            on_progress=lambda i, t, n: calls.append((i, t, n)),
        )

        assert calls == [(1, 1, "260829_1000_new.uni")]


def test_on_progress_is_optional():
    with tempfile.TemporaryDirectory() as tmp:
        config = _tmp_config(tmp)
        client = FakeClient({"a.uni": _valid_uni_bytes()})
        result = run_sync(config, client=client)  # no on_progress -- should not raise
        assert result.new_synced == ["a.uni"]
