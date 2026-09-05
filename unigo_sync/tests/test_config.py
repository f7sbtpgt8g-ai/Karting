"""Tests for core/config.py -- YAML loading, defaults, and the derived
URL helpers."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core import config as config_module  # noqa: E402
from unigo_sync.core.config import SyncConfig, load_config  # noqa: E402


def test_defaults_used_when_file_missing():
    config = load_config(path="/nonexistent/path/config.yaml")
    assert config.base_url == "http://192.168.4.1"
    assert config.max_retries == 3
    assert config.extra == {}


def test_known_keys_override_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "config.yaml")
        Path(path).write_text("base_url: 'http://10.0.0.1'\nmax_retries: 7\n")
        config = load_config(path)
        assert config.base_url == "http://10.0.0.1"
        assert config.max_retries == 7
        # Unspecified fields keep their dataclass defaults.
        assert config.wifi_ssid_prefix == "unigo-"


def test_unknown_keys_land_in_extra_without_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "config.yaml")
        Path(path).write_text("base_url: 'http://10.0.0.1'\nsome_future_field: 42\n")
        config = load_config(path)
        assert config.base_url == "http://10.0.0.1"
        assert config.extra == {"some_future_field": 42}


def test_empty_file_uses_all_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "config.yaml")
        Path(path).write_text("")
        config = load_config(path)
        assert config == SyncConfig()


def test_filelist_url_joins_base_and_path():
    config = SyncConfig(base_url="http://192.168.4.1/", filelist_path="/file?filelist")
    assert config.filelist_url == "http://192.168.4.1/file?filelist"


def test_download_url_url_encodes_name():
    config = SyncConfig(base_url="http://192.168.4.1", download_path_template="/file?filename={name}")
    url = config.download_url("260829_1441_Barmosen GPS.uni")
    assert url == "http://192.168.4.1/file?filename=260829_1441_Barmosen%20GPS.uni"


def test_default_config_path_uses_package_dir_when_not_frozen(monkeypatch):
    monkeypatch.delattr(config_module.sys, "frozen", raising=False)
    path = config_module._default_config_path()
    assert os.path.normpath(path).endswith(os.path.join("unigo_sync", "config.yaml"))


def test_default_config_path_uses_executable_dir_when_frozen(monkeypatch):
    """PyInstaller sets sys.frozen=True and sys.executable to the running
    .exe -- __file__ is meaningless in that case (points into the onefile
    build's ephemeral extraction dir), so the frozen build must look next
    to the .exe instead, where the installer places config.yaml."""
    monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_module.sys, "executable", os.path.join("C:\\", "Program Files", "UniGoSync", "UniGoSync.exe"))
    path = config_module._default_config_path()
    assert path == os.path.join("C:\\", "Program Files", "UniGoSync", "config.yaml")
