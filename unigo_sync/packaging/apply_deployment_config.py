"""Bake a deployment's Supabase settings into the config.yaml that ships
inside the installer, so an end user never has to edit a config file.

Why this exists as a build step rather than committed values: the three
settings include `supabase_db_url`, a Postgres connection string with a
password in it. Keeping it out of the repository and injecting it from CI
secrets at build time means the credential lives in exactly one place
(GitHub Actions secrets) instead of in git history forever. See
`.github/workflows/build-windows-installer.yml` for the wiring.

Usage:
    python unigo_sync/packaging/apply_deployment_config.py <template> <output>

Reads SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_DB_URL from the
environment. With none of them set it copies the template through
unchanged, so a local build with no secrets still produces a working
installer -- one that falls back to the local SQLite database exactly as
before.

Values are never printed: this runs in CI logs.
"""

from __future__ import annotations

import os
import sys

import yaml

# All three, deliberately. `supabase_url`/`supabase_anon_key` alone switch
# only the *login* to Supabase (telemetry.auth.provider_from_env) while
# accounts, driver profiles and uploads stay on the local SQLite file
# (telemetry.db.has_postgres_configured keys off supabase_db_url) -- which
# signs in successfully and then shows an empty driver list, a confusing
# half-configured state that's worth failing the build over rather than
# shipping.
REQUIRED_KEYS = ("supabase_url", "supabase_anon_key", "supabase_db_url")

_BLOCK_HEADER = """
# ---------------------------------------------------------------------
# Deployment settings, filled in when this installer was built (see
# packaging/apply_deployment_config.py). They point this laptop's login
# and uploads at the shared UniGo platform rather than a local file.
# Edit them only if the deployment itself moves; a reinstall will not
# overwrite what's here.
# ---------------------------------------------------------------------
"""


def deployment_values(env: dict[str, str]) -> dict[str, str]:
    """The configured subset of REQUIRED_KEYS, from environment variables
    named after them in upper case. Blank values count as unset -- an
    unpopulated GitHub Actions secret expands to an empty string, not to
    nothing at all."""
    found = {}
    for key in REQUIRED_KEYS:
        value = (env.get(key.upper()) or "").strip()
        if value:
            found[key] = value
    return found


def render(template_text: str, values: dict[str, str]) -> str:
    """The template with `values` appended as a YAML block. Appending
    rather than rewriting keeps the template's own explanatory comments,
    which are what an end user reads if they ever open this file."""
    if not values:
        return template_text

    existing = yaml.safe_load(template_text) or {}
    clashes = sorted(key for key in values if key in existing)
    if clashes:
        raise SystemExit(
            f"{', '.join(clashes)} already set in the template config -- "
            "appending would produce a duplicate YAML key (the later one silently "
            "wins). Set these either in the template or in the environment, not both."
        )

    # Dumped as a mapping in one go rather than interpolated per key:
    # safe_dump of a bare scalar emits a whole YAML *document* (trailing
    # "..." end marker included), which appends to a file that already has
    # one and makes the result unparseable.
    body = yaml.safe_dump(values, sort_keys=False, default_flow_style=False, allow_unicode=True)
    separator = "" if template_text.endswith("\n") else "\n"
    return template_text + separator + _BLOCK_HEADER + body


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    template_path, output_path = argv[1], argv[2]

    values = deployment_values(os.environ)
    if values and len(values) != len(REQUIRED_KEYS):
        missing = [key.upper() for key in REQUIRED_KEYS if key not in values]
        print(
            "Refusing to build a half-configured installer: "
            f"{', '.join(missing)} not set. Set all of "
            f"{', '.join(k.upper() for k in REQUIRED_KEYS)} or none of them.",
            file=sys.stderr,
        )
        return 1

    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read()

    rendered = render(template_text, values)

    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rendered)

    if values:
        print(f"Baked {len(values)} deployment settings into {output_path}.")
        return 0

    message = (
        f"No deployment settings in the environment; copied {template_path} unchanged. "
        "The installed app will fall back to a local SQLite database, so end users will "
        "have to edit config.yaml themselves before they can sign in. Set the "
        f"{'/'.join(k.upper() for k in REQUIRED_KEYS)} repository secrets to avoid this."
    )
    # Surfaced as a build annotation rather than just a log line: shipping
    # an installer nobody can sign into is the exact failure this step
    # exists to prevent, and it is invisible until someone runs the .exe.
    print(f"::warning::{message}" if os.environ.get("GITHUB_ACTIONS") else message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
