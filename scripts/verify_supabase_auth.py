#!/usr/bin/env python
"""Verify Supabase Auth + RLS against the real project.

`tests/test_rls_policies.py` proves the policies behave correctly against a
local Postgres with Supabase's defaults simulated, and
`tests/test_supabase_auth_provider.py` proves the provider links accounts
correctly against a stubbed GoTrue. Neither can prove anything about *your*
project: whether the migrations are actually applied there, whether
`anon`/`authenticated` hold the grants this assumes, or whether GoTrue is
configured the way the provider expects. This closes that gap.

It signs a throwaway account in through real GoTrue, checks the local
`users` row got linked, then queries PostgREST with that account's own JWT
to confirm RLS answers the way the tests say it should.

    export SUPABASE_URL=https://<ref>.supabase.co
    export SUPABASE_ANON_KEY=<anon key>
    export SUPABASE_DB_URL=postgresql://...      # optional, for the local mirror check
    python scripts/verify_supabase_auth.py

Creates one account (a random @example-invalid address). Nothing is deleted
automatically -- remove it from the Supabase dashboard afterwards if you
care. Safe to run against a project with real data: it only ever reads.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, WARN = "  [OK ]", "  [FAIL]", "  [WARN]"
_failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {label}{(' -- ' + detail) if detail else ''}")
    if not ok:
        _failures.append(label)
    return ok


def _request(url: str, key: str, method="GET", payload=None, jwt=None) -> tuple[int, dict | list]:
    headers = {"apikey": key, "Content-Type": "application/json"}
    headers["Authorization"] = f"Bearer {jwt or key}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body) if body else {}
        except ValueError:
            return exc.code, {"raw": body}


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_ANON_KEY first.", file=sys.stderr)
        return 2

    email = f"rls-check-{uuid.uuid4().hex[:10]}@example-invalid.com"
    password = "verify-" + uuid.uuid4().hex

    print(f"Project: {url}")
    print(f"Throwaway account: {email}\n")

    print("GoTrue")
    status, body = _request(f"{url}/auth/v1/signup", key, "POST", {"email": email, "password": password})
    signed_up = check(status in (200, 201), "signup accepted", f"HTTP {status} {str(body)[:120]}")
    if not signed_up:
        print("\nCannot continue without an account.")
        return 1

    external_id = (body.get("user") or body).get("id")
    check(bool(external_id), "signup returned a user id", str(external_id))

    jwt = body.get("access_token")
    if not jwt:
        status, body = _request(
            f"{url}/auth/v1/token?grant_type=password", key, "POST", {"email": email, "password": password}
        )
        jwt = body.get("access_token")
        # Email confirmation being required is a project setting, not a bug --
        # but it does mean this script can't get a JWT to test RLS with.
        if not jwt:
            check(False, "obtained a session JWT", f"HTTP {status} {str(body)[:160]}")
            print(
                "\nIf this project requires email confirmation, disable it temporarily\n"
                "(Authentication > Providers > Email) or confirm the address, then re-run."
            )
            return 1
    check(bool(jwt), "obtained a session JWT")

    print("\nLocal mirror (users.external_auth_id -- what every RLS policy resolves through)")
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        print(f"{WARN} skipped -- set SUPABASE_DB_URL to check this")
    else:
        from telemetry.accounts import account_library_from_env
        from telemetry.auth import auth_store_from_env, provider_from_env

        accounts = account_library_from_env("")
        provider = provider_from_env(accounts, auth_store_from_env(""))
        check(
            provider.name == "supabase",
            "app is configured for Supabase Auth",
            f"active provider: {provider.name}",
        )
        result = provider.login(email, password)
        check(result.ok, "provider.login succeeded", result.error or "")
        if result.ok:
            user = accounts.get_user(result.user_id)
            check(
                user["external_auth_id"] == external_id,
                "local users row linked to the Supabase identity",
                f"external_auth_id={user['external_auth_id']!r}",
            )

    print("\nRLS via PostgREST, as this user")
    rest = f"{url}/rest/v1"

    status, rows = _request(f"{rest}/sessions?select=id,visibility&limit=5", key, jwt=jwt)
    check(status == 200, "can query sessions", f"HTTP {status}")
    if status == 200:
        check(
            isinstance(rows, list) and len(rows) == 0,
            "a brand-new account sees no sessions",
            f"got {len(rows) if isinstance(rows, list) else rows} row(s) -- "
            "anything above 0 means another driver's data is visible to a stranger",
        )

    # The tables 0002 locks down. Any readable row here is account takeover.
    for table in ("auth_tokens", "auth_sessions", "email_outbox"):
        status, rows = _request(f"{rest}/{table}?select=*&limit=1", key, jwt=jwt)
        readable = status == 200 and isinstance(rows, list) and len(rows) > 0
        check(
            not readable,
            f"{table} is not readable by a client",
            f"HTTP {status}" + (" -- APPLY 0002_rls_hardening.sql" if readable else ""),
        )

    status, rows = _request(f"{rest}/users?select=id,email&limit=5", key, jwt=jwt)
    if status == 200 and isinstance(rows, list):
        check(len(rows) <= 1, "users table exposes at most this account", f"{len(rows)} row(s)")

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed -- Supabase Auth links accounts and RLS holds for a real JWT.")
    print(f"Remember to delete the throwaway account {email} from the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
