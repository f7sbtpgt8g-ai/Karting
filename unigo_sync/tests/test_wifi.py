"""Tests for platform_windows/wifi.py -- SSID parsing and prefix
matching. Runs on any OS: `get_current_ssid` is tested against a captured
`netsh` output block, not a live network, and by monkeypatching
subprocess.run rather than requiring an actual Windows machine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.platform_windows import wifi  # noqa: E402

_REAL_NETSH_OUTPUT = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : a1b2c3d4-0000-1111-2222-333344445555
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : unigo-1234
    BSSID                  : 11:22:33:44:55:66
    Network type            : Infrastructure
    Radio type              : 802.11ac
    Authentication          : Open
    Cipher                  : None
    Connectivity mode       : Local only
    Channel                 : 6
    Receive rate (Mbps)     : 65
    Transmit rate (Mbps)    : 65
    Signal                  : 80%
"""


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_get_current_ssid_parses_real_netsh_output(monkeypatch):
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(_REAL_NETSH_OUTPUT)
    )
    assert wifi.get_current_ssid() == "unigo-1234"


def test_get_current_ssid_does_not_false_match_bssid(monkeypatch):
    # BSSID also contains "SSID" as a substring -- make sure the anchored
    # regex only ever matches the actual SSID line.
    output = "    BSSID                  : 11:22:33:44:55:66\n"
    monkeypatch.setattr(wifi.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(output))
    assert wifi.get_current_ssid() is None


def test_get_current_ssid_returns_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: _FakeCompletedProcess("", returncode=1, stderr="no wifi adapter")
    )
    assert wifi.get_current_ssid() is None


def test_get_current_ssid_returns_none_when_netsh_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(wifi.subprocess, "run", _raise)
    assert wifi.get_current_ssid() is None


def test_get_current_ssid_returns_none_on_timeout(monkeypatch):
    import subprocess as real_subprocess

    def _raise(*a, **k):
        raise real_subprocess.TimeoutExpired(cmd="netsh", timeout=10)

    monkeypatch.setattr(wifi.subprocess, "run", _raise)
    assert wifi.get_current_ssid() is None


def test_is_connected_to_unigo_true_for_matching_prefix(monkeypatch):
    monkeypatch.setattr(wifi, "get_current_ssid", lambda: "unigo-1234")
    assert wifi.is_connected_to_unigo() is True


def test_is_connected_to_unigo_false_for_other_networks(monkeypatch):
    monkeypatch.setattr(wifi, "get_current_ssid", lambda: "HomeWiFi")
    assert wifi.is_connected_to_unigo() is False


def test_is_connected_to_unigo_false_when_no_ssid(monkeypatch):
    monkeypatch.setattr(wifi, "get_current_ssid", lambda: None)
    assert wifi.is_connected_to_unigo() is False


def test_is_connected_to_unigo_case_insensitive(monkeypatch):
    monkeypatch.setattr(wifi, "get_current_ssid", lambda: "UniGo-5678")
    assert wifi.is_connected_to_unigo(ssid_prefix="unigo-") is True
