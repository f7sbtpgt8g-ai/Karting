"""Tests for packaging/apply_deployment_config.py -- the build step that
bakes a deployment's Supabase settings into the config.yaml shipped inside
the Windows installer."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.packaging import apply_deployment_config as bake  # noqa: E402

TEMPLATE = """# a comment worth keeping
base_url: "http://192.168.4.1"
# supabase_url: "https://<project>.supabase.co"
"""

FULL_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-key-123",
    "SUPABASE_DB_URL": "postgresql://user:pw@db.example.co:5432/postgres",
}


def test_no_settings_in_the_environment_copies_the_template_through():
    assert bake.render(TEMPLATE, bake.deployment_values({})) == TEMPLATE


def test_all_three_settings_are_appended_and_parse_back():
    rendered = bake.render(TEMPLATE, bake.deployment_values(FULL_ENV))
    parsed = yaml.safe_load(rendered)

    assert parsed["supabase_url"] == FULL_ENV["SUPABASE_URL"]
    assert parsed["supabase_anon_key"] == FULL_ENV["SUPABASE_ANON_KEY"]
    assert parsed["supabase_db_url"] == FULL_ENV["SUPABASE_DB_URL"]
    # The template's own values and comments survive -- an end user opening
    # this file should still find it explained.
    assert parsed["base_url"] == "http://192.168.4.1"
    assert "a comment worth keeping" in rendered


def test_a_connection_string_with_yaml_significant_characters_round_trips():
    """Postgres passwords routinely contain ':', '@' and '#', which are
    exactly the characters that turn an unquoted YAML scalar into something
    else. Dumped through yaml rather than interpolated for that reason."""
    env = dict(FULL_ENV, SUPABASE_DB_URL="postgresql://u:p#a:s@s@db.example.co:5432/postgres")

    parsed = yaml.safe_load(bake.render(TEMPLATE, bake.deployment_values(env)))

    assert parsed["supabase_db_url"] == env["SUPABASE_DB_URL"]


def test_blank_secrets_count_as_unset():
    """An undefined GitHub Actions secret expands to an empty string, not
    to nothing -- treating that as "configured" would ship a config.yaml
    that switches the app to Supabase with no endpoint to talk to."""
    assert bake.deployment_values({k: "" for k in FULL_ENV}) == {}


def test_a_key_already_present_in_the_template_is_refused():
    """Appending would leave two copies of the key in one YAML file, where
    the later silently wins -- fail the build instead."""
    template = TEMPLATE + 'supabase_url: "https://committed.supabase.co"\n'

    with pytest.raises(SystemExit, match="supabase_url"):
        bake.render(template, bake.deployment_values(FULL_ENV))


def test_partial_configuration_fails_the_build(tmp_path, monkeypatch):
    """url+anon_key without db_url signs in against Supabase but reads
    accounts from the local SQLite file -- a successful login followed by
    an empty driver list. Not a shape worth shipping."""
    monkeypatch.setenv("SUPABASE_URL", FULL_ENV["SUPABASE_URL"])
    monkeypatch.setenv("SUPABASE_ANON_KEY", FULL_ENV["SUPABASE_ANON_KEY"])
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    template_path = tmp_path / "config.yaml"
    template_path.write_text(TEMPLATE)
    output_path = tmp_path / "out" / "config.yaml"

    exit_code = bake.main(["prog", str(template_path), str(output_path)])

    assert exit_code == 1
    assert not output_path.exists()


def test_main_writes_the_output_file_and_creates_its_directory(tmp_path, monkeypatch):
    for key, value in FULL_ENV.items():
        monkeypatch.setenv(key, value)
    template_path = tmp_path / "config.yaml"
    template_path.write_text(TEMPLATE)
    output_path = tmp_path / "dist" / "config.yaml"

    exit_code = bake.main(["prog", str(template_path), str(output_path)])

    assert exit_code == 0
    assert yaml.safe_load(output_path.read_text())["supabase_url"] == FULL_ENV["SUPABASE_URL"]


def test_main_does_not_print_secret_values(tmp_path, monkeypatch, capsys):
    for key, value in FULL_ENV.items():
        monkeypatch.setenv(key, value)
    template_path = tmp_path / "config.yaml"
    template_path.write_text(TEMPLATE)

    bake.main(["prog", str(template_path), str(tmp_path / "dist" / "config.yaml")])

    captured = capsys.readouterr()
    assert FULL_ENV["SUPABASE_DB_URL"] not in captured.out + captured.err
    assert FULL_ENV["SUPABASE_ANON_KEY"] not in captured.out + captured.err
