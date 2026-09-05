"""Tests for core/config.py -- YAML loading, defaults, and the derived
URL helpers."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
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
