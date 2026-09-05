"""Tests for core/device_client.py -- no real device needed; the
requests.Session is swapped out for a small fake."""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core.config import SyncConfig  # noqa: E402
from unigo_sync.core.device_client import (  # noqa: E402
    DeviceClient,
    DeviceResponseError,
    DeviceUnreachable,
    UnsafeFilenameError,
    _looks_unsafe,
)


class FakeResponse:
    def __init__(self, json_data=None, content=b"", status_code=200):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data


class FakeSession:
    """Replays a scripted sequence of responses/exceptions, one per call
    to .get(), and counts how many times it was called."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _fast_config(**overrides) -> SyncConfig:
    kwargs = {"max_retries": 3, "retry_backoff_s": 0.001}
    kwargs.update(overrides)
    return SyncConfig(**kwargs)


def test_looks_unsafe_flags_delete_and_update():
    assert _looks_unsafe("file?delete=foo.uni")
    assert _looks_unsafe("firmware_update.bin")
    assert _looks_unsafe("evil\r\nheader-injection")
    assert not _looks_unsafe("260829_1441_Barmosen GPS_AUSTIN.uni")


def test_download_session_refuses_unsafe_name():
    client = DeviceClient(_fast_config())
    client._session = FakeSession([])  # should never be called
    with pytest.raises(UnsafeFilenameError):
        client.download_session("file?delete=all.uni")
    assert client._session.calls == 0


def test_list_sessions_parses_valid_response():
    client = DeviceClient(_fast_config())
    client._session = FakeSession([FakeResponse(json_data={"files": [{"name": "a.uni", "size": "123"}]})])
    sessions = client.list_sessions()
    assert sessions == [{"name": "a.uni", "size": 123}]


def test_list_sessions_raises_on_bad_json_shape():
    client = DeviceClient(_fast_config())
    client._session = FakeSession([FakeResponse(json_data={"unexpected": "shape"})])
    with pytest.raises(DeviceResponseError):
        client.list_sessions()


def test_download_session_returns_bytes_on_success():
    client = DeviceClient(_fast_config())
    client._session = FakeSession([FakeResponse(content=b"raw bytes")])
    assert client.download_session("a.uni") == b"raw bytes"


def test_retries_then_succeeds():
    client = DeviceClient(_fast_config(max_retries=3))
    client._session = FakeSession(
        [requests.ConnectionError("first fail"), requests.ConnectionError("second fail"), FakeResponse(content=b"ok")]
    )
    assert client.download_session("a.uni") == b"ok"
    assert client._session.calls == 3


def test_gives_up_after_max_retries():
    client = DeviceClient(_fast_config(max_retries=2))
    client._session = FakeSession([requests.ConnectionError("fail"), requests.ConnectionError("fail")])
    with pytest.raises(DeviceUnreachable):
        client.download_session("a.uni")
    assert client._session.calls == 2
