"""Authentication: registration, email verification, password reset, and
server-side session management.

Auth is behind an `AuthProvider` interface with two implementations:

- `SupabaseAuthProvider` -- the intended production path. Supabase Auth
  (GoTrue) owns credentials, verification emails and reset emails; this
  class only mirrors the resulting account into the local `users` table so
  `driver_profiles.user_id` has something to point at. **Not exercised
  against a live Supabase project** from the environment this was written
  in (outbound egress to it was blocked), so treat the first real run as
  the integration test -- the request shapes follow GoTrue's documented
  REST API but the error-path handling in particular deserves a look.
- `LocalAuthProvider` -- self-contained: PBKDF2-HMAC-SHA256 password
  hashing, tokens in SQLite, no network. This exists because a managed
  provider cannot be a hard dependency for a tool that is also run on one
  machine at the track with no connectivity, and because it makes every
  flow below testable offline. It is *not* the recommended way to run a
  real multi-user deployment -- prefer Supabase where a network and a
  project exist.

Which one is active is decided by `provider_from_env`: Supabase when
`SUPABASE_URL`/`SUPABASE_ANON_KEY` are set, local otherwise.

Session management is server-side either way (an `auth_sessions` row with
an expiry that can be revoked), so signing out actually invalidates
something rather than just clearing a client-side variable. Note the
Streamlit-specific limitation: the session token lives in
`st.session_state`, which is per-browser-tab and lost on reload, so a
reload means signing in again -- Streamlit has no first-class cookie API to
persist it properly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .accounts import AccountLibrary, is_minor, normalize_email

# OWASP's current floor for PBKDF2-HMAC-SHA256. Deliberately a named
# constant: it is expected to rise over time, and raising it needs a
# rehash-on-next-login path (see `verify_password`'s note).
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16

MIN_PASSWORD_LENGTH = 8

EMAIL_VERIFY_TTL_HOURS = 48
PASSWORD_RESET_TTL_HOURS = 2
LOGIN_SESSION_TTL_DAYS = 7

TOKEN_VERIFY_EMAIL = "verify_email"
TOKEN_PASSWORD_RESET = "password_reset"

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id)
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass
class AuthResult:
    ok: bool
    user_id: int | None = None
    error: str | None = None
    # Set when a flow produced a token the caller must deliver by email
    # (local provider only -- Supabase sends its own).
    token: str | None = None


def validate_password(password: str) -> str | None:
    """Returns an error message, or None if acceptable. Length only, per
    current NIST guidance -- composition rules ("must contain a symbol")
    push people toward predictable substitutions without materially
    improving strength."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time verification. Returns False for a missing hash rather
    than raising, so an account managed by an external provider (no local
    hash) simply fails local password login instead of erroring.

    Does not currently re-hash when `PBKDF2_ITERATIONS` has since been
    raised -- worth adding on the next iteration bump so existing accounts
    are upgraded on their next successful login.
    """
    if not encoded:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(expected.hex(), digest_hex)


class AuthStore:
    """Tokens and login sessions, in the same SQLite file as everything
    else (same connection-per-call convention as `SessionLibrary`)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(AUTH_SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def issue_token(self, user_id: int, kind: str, ttl_hours: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_tokens (user_id, kind, token, expires_at, created_at) VALUES (?,?,?,?,?)",
                (user_id, kind, token, _iso(_now() + timedelta(hours=ttl_hours)), _iso(_now())),
            )
            conn.commit()
        return token

    def consume_token(self, token: str, kind: str) -> int | None:
        """Redeem a token exactly once. Returns the user id, or None if the
        token is unknown, of the wrong kind, expired, or already used."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_tokens WHERE token = ? AND kind = ?", (token, kind)
            ).fetchone()
            if row is None or row["used_at"] is not None:
                return None
            if datetime.fromisoformat(row["expires_at"]) < _now():
                return None
            conn.execute("UPDATE auth_tokens SET used_at = ? WHERE id = ?", (_iso(_now()), row["id"]))
            conn.commit()
            return int(row["user_id"])

    def start_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO auth_sessions (user_id, token, created_at, expires_at) VALUES (?,?,?,?)",
                (user_id, token, _iso(_now()), _iso(_now() + timedelta(days=LOGIN_SESSION_TTL_DAYS))),
            )
            conn.commit()
        return token

    def user_for_session(self, token: str | None) -> int | None:
        if not token:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM auth_sessions WHERE token = ?", (token,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < _now():
            return None
        return int(row["user_id"])

    def revoke_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE auth_sessions SET revoked_at = ? WHERE token = ?", (_iso(_now()), token))
            conn.commit()

    def revoke_all_sessions(self, user_id: int) -> None:
        """Used after a password reset -- a reset should not leave an
        attacker's existing session alive."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (_iso(_now()), user_id),
            )
            conn.commit()


class AuthProvider:
    """Interface shared by the local and Supabase implementations."""

    def register(
        self, email: str, password: str, display_name: str | None = None,
        date_of_birth: str | None = None, guardian_email: str | None = None,
    ) -> AuthResult:
        raise NotImplementedError

    def login(self, email: str, password: str) -> AuthResult:
        raise NotImplementedError

    def request_email_verification(self, user_id: int) -> AuthResult:
        raise NotImplementedError

    def verify_email(self, token: str) -> AuthResult:
        raise NotImplementedError

    def request_password_reset(self, email: str) -> AuthResult:
        raise NotImplementedError

    def reset_password(self, token: str, new_password: str) -> AuthResult:
        raise NotImplementedError


class LocalAuthProvider(AuthProvider):
    """Self-contained auth for offline/local deployments and tests."""

    name = "local"

    def __init__(self, accounts: AccountLibrary, store: AuthStore):
        self.accounts = accounts
        self.store = store

    def register(
        self, email: str, password: str, display_name: str | None = None,
        date_of_birth: str | None = None, guardian_email: str | None = None,
    ) -> AuthResult:
        email = normalize_email(email)
        if problem := validate_password(password):
            return AuthResult(False, error=problem)
        if self.accounts.get_user_by_email(email) is not None:
            return AuthResult(False, error="An account with that email already exists.")
        if is_minor(date_of_birth) and not guardian_email:
            return AuthResult(
                False, error="A parent or guardian's email address is required to register under 16.",
            )

        user_id, _profile_id = self.accounts.register_user_with_profile(
            email, password_hash=hash_password(password), display_name=display_name,
            date_of_birth=date_of_birth, guardian_email=guardian_email,
        )
        token = self.store.issue_token(user_id, TOKEN_VERIFY_EMAIL, EMAIL_VERIFY_TTL_HOURS)
        return AuthResult(True, user_id=user_id, token=token)

    def login(self, email: str, password: str) -> AuthResult:
        user = self.accounts.get_user_by_email(email)
        # Same message whether the account is missing or the password is
        # wrong, so this can't be used to enumerate registered emails.
        if user is None or not verify_password(password, user["password_hash"]):
            return AuthResult(False, error="Incorrect email or password.")
        self.accounts.record_login(int(user["id"]))
        return AuthResult(True, user_id=int(user["id"]))

    def request_email_verification(self, user_id: int) -> AuthResult:
        token = self.store.issue_token(user_id, TOKEN_VERIFY_EMAIL, EMAIL_VERIFY_TTL_HOURS)
        return AuthResult(True, user_id=user_id, token=token)

    def verify_email(self, token: str) -> AuthResult:
        user_id = self.store.consume_token(token, TOKEN_VERIFY_EMAIL)
        if user_id is None:
            return AuthResult(False, error="That verification link is invalid or has expired.")
        self.accounts.set_email_verified(user_id, True)
        return AuthResult(True, user_id=user_id)

    def request_password_reset(self, email: str) -> AuthResult:
        user = self.accounts.get_user_by_email(email)
        if user is None:
            # Reported as success on purpose: telling an anonymous caller
            # whether an address is registered is an account-enumeration
            # hole. No token is issued, so nothing is actually sent.
            return AuthResult(True, error=None)
        token = self.store.issue_token(int(user["id"]), TOKEN_PASSWORD_RESET, PASSWORD_RESET_TTL_HOURS)
        return AuthResult(True, user_id=int(user["id"]), token=token)

    def reset_password(self, token: str, new_password: str) -> AuthResult:
        if problem := validate_password(new_password):
            return AuthResult(False, error=problem)
        user_id = self.store.consume_token(token, TOKEN_PASSWORD_RESET)
        if user_id is None:
            return AuthResult(False, error="That reset link is invalid or has expired.")
        self.accounts.set_password_hash(user_id, hash_password(new_password))
        self.store.revoke_all_sessions(user_id)
        return AuthResult(True, user_id=user_id)


class SupabaseAuthProvider(AuthProvider):
    """Delegates credentials, verification and reset emails to Supabase
    Auth (GoTrue), mirroring the account locally so driver profiles and
    session ownership have a `users` row to reference.

    Written against GoTrue's documented REST API but never run against a
    live project here -- see the module docstring.
    """

    name = "supabase"

    def __init__(self, accounts: AccountLibrary, store: AuthStore, url: str, anon_key: str, timeout_s: float = 10.0):
        self.accounts = accounts
        self.store = store
        self.url = url.rstrip("/")
        self.anon_key = anon_key
        self.timeout_s = timeout_s

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.url}/auth/v1{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "apikey": self.anon_key,
                "Authorization": f"Bearer {self.anon_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode() or "{}")
            except ValueError:
                return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, {"error_description": f"Could not reach the authentication service ({exc})."}

    @staticmethod
    def _error_text(body: dict, fallback: str) -> str:
        return body.get("error_description") or body.get("msg") or body.get("message") or fallback

    def _mirror_user(self, external_id: str, email: str, **profile) -> int:
        """Find or create the local `users` row for a Supabase account.
        Registration also creates the linked driver profile, matching the
        local provider's behavior so the common case stays invisible."""
        existing = self.accounts.get_user_by_external_auth_id(external_id)
        if existing is not None:
            return int(existing["id"])
        by_email = self.accounts.get_user_by_email(email)
        if by_email is not None:
            return int(by_email["id"])
        user_id, _ = self.accounts.register_user_with_profile(
            email, external_auth_id=external_id, **profile
        )
        return user_id

    def register(
        self, email: str, password: str, display_name: str | None = None,
        date_of_birth: str | None = None, guardian_email: str | None = None,
    ) -> AuthResult:
        email = normalize_email(email)
        if problem := validate_password(password):
            return AuthResult(False, error=problem)
        if is_minor(date_of_birth) and not guardian_email:
            return AuthResult(
                False, error="A parent or guardian's email address is required to register under 16.",
            )
        status, body = self._post("/signup", {"email": email, "password": password})
        if status not in (200, 201):
            return AuthResult(False, error=self._error_text(body, "Registration failed."))

        external_id = (body.get("user") or body).get("id")
        if not external_id:
            return AuthResult(False, error="Authentication service returned an unexpected response.")
        user_id = self._mirror_user(
            external_id, email, display_name=display_name,
            date_of_birth=date_of_birth, guardian_email=guardian_email,
        )
        # Supabase sends its own verification email; no local token.
        return AuthResult(True, user_id=user_id)

    def login(self, email: str, password: str) -> AuthResult:
        email = normalize_email(email)
        status, body = self._post("/token?grant_type=password", {"email": email, "password": password})
        if status != 200:
            return AuthResult(False, error=self._error_text(body, "Incorrect email or password."))
        user = body.get("user") or {}
        external_id = user.get("id")
        if not external_id:
            return AuthResult(False, error="Authentication service returned an unexpected response.")
        user_id = self._mirror_user(external_id, email)
        # Supabase is the source of truth for verification state.
        if user.get("email_confirmed_at") or user.get("confirmed_at"):
            self.accounts.set_email_verified(user_id, True)
        self.accounts.record_login(user_id)
        return AuthResult(True, user_id=user_id)

    def request_email_verification(self, user_id: int) -> AuthResult:
        user = self.accounts.get_user(user_id)
        if user is None:
            return AuthResult(False, error="No such account.")
        status, body = self._post("/resend", {"type": "signup", "email": user["email"]})
        if status not in (200, 201):
            return AuthResult(False, error=self._error_text(body, "Could not resend the verification email."))
        return AuthResult(True, user_id=user_id)

    def verify_email(self, token: str) -> AuthResult:
        # Verification links point at Supabase, which confirms the address
        # and redirects back; there is no local token to consume.
        return AuthResult(
            False, error="Email verification is handled by the authentication service's own link.",
        )

    def request_password_reset(self, email: str) -> AuthResult:
        status, body = self._post("/recover", {"email": normalize_email(email)})
        if status not in (200, 201):
            return AuthResult(False, error=self._error_text(body, "Could not start a password reset."))
        return AuthResult(True)

    def reset_password(self, token: str, new_password: str) -> AuthResult:
        return AuthResult(
            False, error="Password resets are completed through the authentication service's own link.",
        )


def provider_from_env(accounts: AccountLibrary, store: AuthStore) -> AuthProvider:
    """Supabase when configured, local otherwise. Reading this from the
    environment (rather than a settings toggle) keeps the deployed
    configuration a deployment concern -- there is no way for a user of the
    running app to switch the auth backend."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_ANON_KEY")
    if url and key:
        return SupabaseAuthProvider(accounts, store, url, key)
    return LocalAuthProvider(accounts, store)
