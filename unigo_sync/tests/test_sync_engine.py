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
from unigo_sync.core.sync_engine import run_sync  # noqa: E402
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
