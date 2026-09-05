"""Account, driver-identity, attribution, and visibility data layer.

The central design decision here is that a **driver is not an account**:

- A `User` row is an authenticated login (email, verification state, and --
  only for the offline/local auth backend -- a password hash). See `auth.py`.
- A `DriverProfile` row is *a person whose telemetry can exist in the
  system*. It may have no `User` at all (an "unclaimed" profile, created by
  someone uploading data on that driver's behalf) or be linked to exactly
  one `User` once claimed.
- A telemetry session is owned by a `DriverProfile`, and *separately*
  records the `User` who actually uploaded it. Those are genuinely
  different questions -- a team manager uploading a shared logger's file
  attributes each session to a different driver -- and keeping them as two
  fields from the start is what makes attribution, claiming, and the
  privacy rules below expressible at all.

Everything that decides *who may see what* funnels through
`PUBLIC_VISIBILITY_SQL` / `session_is_publicly_visible`. That is deliberate:
the rule that an unclaimed profile's data can never reach a leaderboard or
be selected as another driver's comparison reference is a hard gate, not a
default someone can override, and a single shared predicate is what stops
that guarantee from drifting apart between the leaderboard query, the
comparison browser, and any view added later.

Sessions are shared by default (`VISIBILITY_DEFAULT`) and can be unshared
one toggle at a time. Note the asymmetry that creates, and why it is
intentional: a *claimed* profile's owner is present to opt out, so
defaulting them in is a reversible choice they control. An *unclaimed*
profile has nobody who can opt out on their behalf, so the claim gate keeps
their data out of every public surface until they have an account and can
decide for themselves.

Lives alongside `storage.py` in the same SQLite file (one database, one
connection-per-call convention -- see `SessionLibrary`'s docstring for why
connections are not held open).
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import db as pgdb

# Claim links are single-purpose and emailed to someone who has not yet
# consented to being on the platform at all, so they expire on the shorter
# side rather than lingering in an inbox indefinitely.
CLAIM_TOKEN_TTL_DAYS = 14

# Age below which a guardian consent flow is required before the account is
# usable. 16 is the GDPR Article 8 default; several EU member states set it
# as low as 13, and COPPA uses 13 in the US. 16 is the conservative choice
# and is the one to revisit with counsel per jurisdiction -- it is stored as
# a named constant precisely because it is a policy decision, not a fact.
PARENTAL_CONSENT_AGE = 16

CLAIM_UNCLAIMED = "unclaimed"
CLAIM_INVITED = "invited"
CLAIM_CLAIMED = "claimed"

VISIBILITY_PRIVATE = "private"
VISIBILITY_SHARED = "shared"

# New sessions are shared by default: the comparison and leaderboard
# features are only worth anything if there is something in the pool to
# compare against, and a default of private leaves every new driver looking
# at an empty board. Unsharing is a single toggle per session, and the
# `PUBLIC_VISIBILITY_SQL` gate below still applies -- in particular a
# driver who has no account yet cannot be opted in by anyone else, since
# "you can always unshare" is meaningless for someone with no way to.
VISIBILITY_DEFAULT = VISIBILITY_SHARED

ATTRIBUTION_CONFIRMED = "confirmed"
ATTRIBUTION_PENDING = "pending_confirmation"
ATTRIBUTION_REJECTED = "rejected"

CONSENT_NOT_REQUIRED = "not_required"
CONSENT_PENDING = "pending"
CONSENT_GRANTED = "granted"
CONSENT_DENIED = "denied"

ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    -- The managed auth provider's own user id (e.g. a Supabase UUID) when
    -- one is in use. NULL for accounts created by the offline/local
    -- backend, which is what keeps a single-machine deployment working
    -- without provisioning an external service.
    external_auth_id TEXT UNIQUE,
    -- Only ever populated by the local backend; a managed provider never
    -- hands us the password and this stays NULL.
    password_hash TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name TEXT,
    date_of_birth TEXT,
    guardian_email TEXT,
    guardian_consent_status TEXT NOT NULL DEFAULT 'not_required',
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS driver_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    -- NULL while unclaimed; exactly one user once claimed (enforced UNIQUE
    -- so a single account can never end up owning two driver identities by
    -- accident, which would silently split one driver's history in two).
    user_id INTEGER UNIQUE,
    claim_status TEXT NOT NULL DEFAULT 'unclaimed',
    invite_email TEXT,
    claim_token TEXT UNIQUE,
    claim_token_expires_at TEXT,
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (created_by_user_id) REFERENCES users (id)
);

-- A cross-account attribution awaiting the target driver's say-so. Filing
-- someone else's session straight into their history without asking would
-- make one account able to write into another's record, so an upload
-- attributed to an already-registered driver lands here first.
CREATE TABLE IF NOT EXISTS attribution_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_db_id INTEGER NOT NULL,
    target_driver_profile_id INTEGER NOT NULL,
    requested_by_user_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (session_db_id) REFERENCES sessions (id),
    FOREIGN KEY (target_driver_profile_id) REFERENCES driver_profiles (id),
    FOREIGN KEY (requested_by_user_id) REFERENCES users (id)
);

-- An unprompted claim: someone who registered independently spotting an
-- unclaimed placeholder they believe is them. Kept deliberately lightweight
-- (see `request_profile_claim`) -- the uploader is notified rather than
-- being made an approval gate.
CREATE TABLE IF NOT EXISTS profile_claim_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_profile_id INTEGER NOT NULL,
    requested_by_user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (driver_profile_id) REFERENCES driver_profiles (id),
    FOREIGN KEY (requested_by_user_id) REFERENCES users (id)
);

-- Reported misattribution -- the escape hatch that lets claiming stay
-- lightweight instead of needing a full dispute-resolution system upfront.
CREATE TABLE IF NOT EXISTS attribution_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_db_id INTEGER,
    driver_profile_id INTEGER,
    reported_by_user_id INTEGER,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
"""

# The one and only definition of "this session may be seen by someone other
# than its owner and uploader". Written as a SQL fragment so the leaderboard
# and the shared-lap browser filter on exactly the same rule the row-level
# check uses, rather than two hand-written WHERE clauses that can drift.
#
# Requires `sessions s` JOINed to `driver_profiles p` on p.id =
# s.driver_profile_id. All three conditions are load-bearing:
#   1. the driver explicitly opted this session in to sharing;
#   2. the attribution is settled (a pending or rejected one is not yet, or
#      never was, this driver's data to share);
#   3. the owning profile is genuinely claimed by a real account -- an
#      unclaimed driver has had no opportunity to set a sharing preference,
#      so no uploader's choice on their behalf can make their data public.
PUBLIC_VISIBILITY_SQL = (
    "s.visibility = 'shared' "
    "AND s.attribution_status = 'confirmed' "
    "AND p.claim_status = 'claimed' "
    "AND p.user_id IS NOT NULL"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def is_minor(date_of_birth: str | None, today: date | None = None) -> bool:
    """Whether a date of birth (ISO `YYYY-MM-DD`) puts someone under
    `PARENTAL_CONSENT_AGE`. An unknown DOB returns False rather than
    guessing -- callers that must not proceed without knowing should check
    for a missing DOB explicitly instead of relying on this."""
    if not date_of_birth:
        return False
    try:
        dob = date.fromisoformat(date_of_birth)
    except ValueError:
        return False
    today = today or datetime.now(timezone.utc).date()
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years < PARENTAL_CONSENT_AGE


@dataclass
class DriverProfile:
    id: int
    display_name: str
    user_id: int | None
    claim_status: str
    invite_email: str | None
    created_by_user_id: int | None

    @property
    def is_claimed(self) -> bool:
        return self.claim_status == CLAIM_CLAIMED and self.user_id is not None


class AccountLibrary:
    """Accounts, driver identities, attribution and visibility, sharing the
    SQLite file `SessionLibrary` uses. Follows the same
    connection-per-call convention as `SessionLibrary` (see its docstring:
    a long-lived connection shared across Streamlit reruns was found to
    hang)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(ACCOUNTS_SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- users

    def create_user(
        self,
        email: str,
        password_hash: str | None = None,
        external_auth_id: str | None = None,
        display_name: str | None = None,
        date_of_birth: str | None = None,
        guardian_email: str | None = None,
        email_verified: bool = False,
    ) -> int:
        """Create a login account. Does **not** create a driver profile --
        callers that want the ordinary self-registration behavior should use
        `register_user_with_profile`, which is the path that keeps the
        common case invisible plumbing rather than an extra step."""
        consent = CONSENT_PENDING if is_minor(date_of_birth) else CONSENT_NOT_REQUIRED
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO users
                   (email, external_auth_id, password_hash, email_verified, display_name,
                    date_of_birth, guardian_email, guardian_consent_status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    normalize_email(email), external_auth_id, password_hash, int(email_verified),
                    display_name, date_of_birth, guardian_email, consent, _now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def register_user_with_profile(
        self,
        email: str,
        password_hash: str | None = None,
        external_auth_id: str | None = None,
        display_name: str | None = None,
        date_of_birth: str | None = None,
        guardian_email: str | None = None,
        email_verified: bool = False,
    ) -> tuple[int, int]:
        """Ordinary self-service registration: create the account *and* the
        driver profile it owns, linked. Returns `(user_id,
        driver_profile_id)`."""
        user_id = self.create_user(
            email, password_hash=password_hash, external_auth_id=external_auth_id,
            display_name=display_name, date_of_birth=date_of_birth,
            guardian_email=guardian_email, email_verified=email_verified,
        )
        profile_id = self.create_profile_for_user(user_id, display_name or normalize_email(email))
        return user_id, profile_id

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
        return dict(row) if row else None

    def get_user_by_external_auth_id(self, external_auth_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE external_auth_id = ?", (external_auth_id,)).fetchone()
        return dict(row) if row else None

    def set_email_verified(self, user_id: int, verified: bool = True) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET email_verified = ? WHERE id = ?", (int(verified), user_id))
            conn.commit()

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
            conn.commit()

    def set_guardian_consent(self, user_id: int, status: str, guardian_email: str | None = None) -> None:
        with self._connect() as conn:
            if guardian_email is not None:
                conn.execute(
                    "UPDATE users SET guardian_consent_status = ?, guardian_email = ? WHERE id = ?",
                    (status, normalize_email(guardian_email), user_id),
                )
            else:
                conn.execute("UPDATE users SET guardian_consent_status = ? WHERE id = ?", (status, user_id))
            conn.commit()

    def record_login(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), user_id))
            conn.commit()

    def account_is_usable(self, user_id: int) -> tuple[bool, str | None]:
        """Whether an account may actually be used, and if not, why. A minor
        whose guardian has not granted consent is blocked here rather than
        at each individual feature, so no path can accidentally skip it."""
        user = self.get_user(user_id)
        if user is None:
            return False, "No such account."
        if not user["email_verified"]:
            return False, "Email address not verified yet."
        if is_minor(user["date_of_birth"]) and user["guardian_consent_status"] != CONSENT_GRANTED:
            return False, "Parent/guardian consent is required before this account can be used."
        return True, None

    # ------------------------------------------------------- driver profiles

    def create_profile_for_user(self, user_id: int, display_name: str) -> int:
        """A claimed profile belonging to an account from the outset -- the
        self-registration case."""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO driver_profiles
                   (display_name, user_id, claim_status, created_at, claimed_at)
                   VALUES (?,?,?,?,?)""",
                (display_name, user_id, CLAIM_CLAIMED, _now(), _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def create_unclaimed_profile(
        self, display_name: str, created_by_user_id: int | None = None, invite_email: str | None = None
    ) -> tuple[int, str | None]:
        """A driver profile with no account behind it yet.

        With an `invite_email`, the profile is marked `invited` and a claim
        token is issued for the invite link. Without one it is a silent
        placeholder: no token, no outreach, no contact of any kind -- it
        exists purely so the uploader's own records and comparisons can
        refer to that driver. Returns `(profile_id, claim_token or None)`.
        """
        token = secrets.token_urlsafe(32) if invite_email else None
        expires = (datetime.now(timezone.utc) + timedelta(days=CLAIM_TOKEN_TTL_DAYS)).isoformat() if token else None
        status = CLAIM_INVITED if invite_email else CLAIM_UNCLAIMED
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO driver_profiles
                   (display_name, user_id, claim_status, invite_email, claim_token,
                    claim_token_expires_at, created_by_user_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    display_name, None, status, normalize_email(invite_email) if invite_email else None,
                    token, expires, created_by_user_id, _now(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid), token

    def get_profile(self, profile_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM driver_profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def get_profile_for_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM driver_profiles WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_profiles(self, claim_status: str | None = None, name_query: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM driver_profiles WHERE 1=1"
        params: tuple = ()
        if claim_status is not None:
            query += " AND claim_status = ?"
            params += (claim_status,)
        if name_query:
            query += " AND LOWER(display_name) LIKE ?"
            params += (f"%{name_query.strip().lower()}%",)
        query += " ORDER BY display_name"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def list_registered_drivers(self, name_query: str | None = None) -> pd.DataFrame:
        """Claimed profiles only -- the searchable set for "attribute this
        session to an existing registered driver"."""
        return self.list_profiles(claim_status=CLAIM_CLAIMED, name_query=name_query)

    # ------------------------------------------------------------ claiming

    def get_profile_by_claim_token(self, token: str) -> dict | None:
        """The invited profile a claim link points at, or None if the token
        is unknown, already used, or expired."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM driver_profiles WHERE claim_token = ?", (token,)).fetchone()
        if row is None:
            return None
        profile = dict(row)
        if profile["claim_status"] == CLAIM_CLAIMED:
            return None
        expires = profile.get("claim_token_expires_at")
        if expires and datetime.fromisoformat(expires) < datetime.now(timezone.utc):
            return None
        return profile

    def claim_profile(self, profile_id: int, user_id: int) -> None:
        """Link an unclaimed profile to a (just-registered or existing)
        account. Every session already attributed to the profile becomes
        that user's immediately and with no data movement, because sessions
        reference the *profile*, never the account -- which is the whole
        reason those are separate entities.

        Raises if the profile is already claimed, or if the account already
        owns a different profile: silently merging two driver identities
        would be far worse than refusing, since it is not reversible and
        would conflate two people's histories."""
        profile = self.get_profile(profile_id)
        if profile is None:
            raise KeyError(f"No driver profile {profile_id}")
        if profile["claim_status"] == CLAIM_CLAIMED:
            raise ValueError("That driver profile has already been claimed.")
        existing = self.get_profile_for_user(user_id)
        if existing is not None and existing["id"] != profile_id:
            raise ValueError(
                "This account already has a driver profile. Merging two driver identities isn't supported -- "
                "report an incorrect attribution instead so it can be sorted out manually."
            )
        with self._connect() as conn:
            conn.execute(
                """UPDATE driver_profiles
                   SET user_id = ?, claim_status = ?, claimed_at = ?, claim_token = NULL,
                       claim_token_expires_at = NULL
                   WHERE id = ?""",
                (user_id, CLAIM_CLAIMED, _now(), profile_id),
            )
            conn.commit()

    def claim_profile_by_token(self, token: str, user_id: int) -> int:
        """Claim via an invite link. Returns the claimed profile's id."""
        profile = self.get_profile_by_claim_token(token)
        if profile is None:
            raise ValueError("That claim link is invalid, already used, or expired.")
        self.claim_profile(int(profile["id"]), user_id)
        return int(profile["id"])

    def request_profile_claim(self, profile_id: int, user_id: int, note: str | None = None) -> int:
        """An unprompted claim ("that placeholder is me") by someone who
        registered without ever following an invite link.

        Deliberately lightweight for v1: this records the claim and notifies
        the uploader rather than blocking on their approval. A hard approval
        gate would strand a real driver behind an uploader who has moved on,
        and a full dispute system is not worth building before there is any
        evidence of disputes -- `report_attribution` is the safety valve
        instead."""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO profile_claim_requests (driver_profile_id, requested_by_user_id, note, created_at)
                   VALUES (?,?,?,?)""",
                (profile_id, user_id, note, _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def report_attribution(
        self, reported_by_user_id: int, session_db_id: int | None = None,
        driver_profile_id: int | None = None, reason: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO attribution_reports
                   (session_db_id, driver_profile_id, reported_by_user_id, reason, created_at)
                   VALUES (?,?,?,?,?)""",
                (session_db_id, driver_profile_id, reported_by_user_id, reason, _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    # --------------------------------------------------------- attribution

    def attribute_session(
        self, session_db_id: int, driver_profile_id: int, uploaded_by_user_id: int | None,
        requires_confirmation: bool = False, message: str | None = None,
    ) -> int | None:
        """Point a stored session at the driver it belongs to.

        `requires_confirmation` is for the cross-account case (attributing
        to someone else's already-registered profile): the session records
        the intended owner but stays `pending_confirmation`, which every
        history//sharing/leaderboard query treats as not-yet-theirs, until
        that driver accepts. Returns the attribution request's id in that
        case, else None.
        """
        status = ATTRIBUTION_PENDING if requires_confirmation else ATTRIBUTION_CONFIRMED
        with self._connect() as conn:
            conn.execute(
                """UPDATE sessions
                   SET driver_profile_id = ?, uploaded_by_user_id = ?, attribution_status = ?
                   WHERE id = ?""",
                (driver_profile_id, uploaded_by_user_id, status, session_db_id),
            )
            request_id = None
            if requires_confirmation:
                cur = conn.execute(
                    """INSERT INTO attribution_requests
                       (session_db_id, target_driver_profile_id, requested_by_user_id, message, created_at)
                       VALUES (?,?,?,?,?)""",
                    (session_db_id, driver_profile_id, uploaded_by_user_id, message, _now()),
                )
                request_id = int(cur.lastrowid)
            conn.commit()
        return request_id

    def pending_attribution_requests(self, driver_profile_id: int) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                """SELECT r.*, s.source_file, s.session_index, s.start_date, s.start_time,
                          s.track_name, s.best_lap_s, s.n_laps, u.email AS requested_by_email
                   FROM attribution_requests r
                   JOIN sessions s ON s.id = r.session_db_id
                   LEFT JOIN users u ON u.id = r.requested_by_user_id
                   WHERE r.target_driver_profile_id = ? AND r.status = 'pending'
                   ORDER BY r.created_at""",
                conn, params=(driver_profile_id,),
            )

    def resolve_attribution_request(self, request_id: int, accept: bool) -> None:
        """Accept files the session into the target driver's history;
        reject detaches it and leaves it with the uploader to re-attribute,
        rather than either dumping it in the rejecting driver's records or
        destroying an upload they simply said wasn't theirs."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM attribution_requests WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                raise KeyError(f"No attribution request {request_id}")
            if accept:
                conn.execute(
                    "UPDATE sessions SET attribution_status = ? WHERE id = ?",
                    (ATTRIBUTION_CONFIRMED, row["session_db_id"]),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET driver_profile_id = NULL, attribution_status = ? WHERE id = ?",
                    (ATTRIBUTION_REJECTED, row["session_db_id"]),
                )
            conn.execute(
                "UPDATE attribution_requests SET status = ?, resolved_at = ? WHERE id = ?",
                ("accepted" if accept else "rejected", _now(), request_id),
            )
            conn.commit()

    # ---------------------------------------------------------- visibility

    def set_session_visibility(self, session_db_id: int, visibility: str) -> None:
        if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED):
            raise ValueError(f"Unknown visibility {visibility!r}")
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET visibility = ? WHERE id = ?", (visibility, session_db_id))
            conn.commit()

    def session_is_publicly_visible(self, session_db_id: int) -> bool:
        """Row-level form of `PUBLIC_VISIBILITY_SQL` -- same predicate, so a
        single session's answer can never disagree with what the list
        queries below would have included."""
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT 1 FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE s.id = ? AND {PUBLIC_VISIBILITY_SQL}""",
                (session_db_id,),
            ).fetchone()
        return row is not None

    def visible_sessions_for_user(self, user_id: int | None) -> pd.DataFrame:
        """Every session a given account may open: their own driver
        profile's confirmed sessions, anything they uploaded themselves
        (including placeholders they created and attributions still awaiting
        someone else's confirmation), plus everything publicly shared by
        other claimed drivers."""
        with self._connect() as conn:
            return pd.read_sql_query(
                f"""SELECT s.*, p.display_name AS driver_display_name, p.claim_status
                    FROM sessions s
                    LEFT JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE (p.user_id IS NOT NULL AND p.user_id = ? AND s.attribution_status = 'confirmed')
                       OR s.uploaded_by_user_id = ?
                       OR ({PUBLIC_VISIBILITY_SQL})
                    ORDER BY s.start_date, s.start_time""",
                conn, params=(user_id, user_id),
            )

    def sessions_for_profile(self, driver_profile_id: int, include_pending: bool = False) -> pd.DataFrame:
        query = "SELECT * FROM sessions WHERE driver_profile_id = ?"
        if not include_pending:
            query += " AND attribution_status = 'confirmed'"
        query += " ORDER BY start_date, start_time"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=(driver_profile_id,))

    def shareable_reference_sessions(
        self, exclude_user_id: int | None = None, track_name: str | None = None,
        driver_query: str | None = None, kart_class: str | None = None,
        track_condition: str | None = None, start_date_from: str | None = None,
        start_date_to: str | None = None,
    ) -> pd.DataFrame:
        """Sessions another driver may select as a comparison reference --
        the shared-lap browser's query. Eligibility is the shared public
        gate, so an unclaimed profile's data can never appear here however
        the uploader marked it."""
        query = f"""
            SELECT s.*, p.display_name AS driver_display_name
            FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE {PUBLIC_VISIBILITY_SQL}
        """
        params: tuple = ()
        if exclude_user_id is not None:
            query += " AND (p.user_id IS NULL OR p.user_id != ?)"
            params += (exclude_user_id,)
        if track_name:
            query += " AND s.track_name = ?"
            params += (track_name,)
        if driver_query:
            query += " AND LOWER(p.display_name) LIKE ?"
            params += (f"%{driver_query.strip().lower()}%",)
        if kart_class:
            query += " AND s.kart_class = ?"
            params += (kart_class,)
        if track_condition:
            query += " AND s.track_condition = ?"
            params += (track_condition,)
        if start_date_from:
            query += " AND s.start_date >= ?"
            params += (start_date_from,)
        if start_date_to:
            query += " AND s.start_date <= ?"
            params += (start_date_to,)
        query += " ORDER BY s.best_lap_s"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    # -------------------------------------------------------- leaderboards

    def leaderboard(
        self, track_name: str, track_condition: str | None = None, kart_class: str | None = None,
        limit: int = 50,
    ) -> pd.DataFrame:
        """Best lap per driver at one track, fastest first.

        `track_condition=None` is the "Overall" filter: every condition
        pooled, ranked on time alone. Eligibility is the same shared public
        gate as everywhere else -- a private session never appears, and
        neither does an unclaimed profile's, regardless of how it was
        marked.

        One row per driver (their single best qualifying lap), so a driver
        with more track days can't crowd out the board.
        """
        query = f"""
            SELECT p.id AS driver_profile_id, p.display_name AS driver_display_name,
                   MIN(s.best_lap_s) AS best_lap_s, s.track_name,
                   COUNT(*) AS qualifying_sessions
            FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE {PUBLIC_VISIBILITY_SQL}
              AND s.track_name = ? AND s.best_lap_s IS NOT NULL
        """
        params: tuple = (track_name,)
        if track_condition:
            query += " AND s.track_condition = ?"
            params += (track_condition,)
        if kart_class:
            query += " AND s.kart_class = ?"
            params += (kart_class,)
        query += " GROUP BY p.id, p.display_name, s.track_name ORDER BY best_lap_s LIMIT ?"
        params += (limit,)
        with self._connect() as conn:
            board = pd.read_sql_query(query, conn, params=params)
        if not board.empty:
            board.insert(0, "rank", range(1, len(board) + 1))
        return board

    def community_stats(self) -> dict:
        """Headline numbers for the shared pool -- how many sessions,
        drivers and tracks are actually available to compare against.

        Used to show what sharing is *for*, rather than nagging people into
        it: an empty pool is the honest reason a new driver's leaderboard
        and comparison pages look bare."""
        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS shared_sessions,
                           COUNT(DISTINCT p.id) AS drivers,
                           COUNT(DISTINCT s.track_name) AS tracks
                    FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL}"""
            ).fetchone()
        return dict(row) if row else {"shared_sessions": 0, "drivers": 0, "tracks": 0}

    def driver_contribution(self, driver_profile_id: int) -> dict:
        """This driver's own share/withhold split, plus how many *other*
        drivers' sessions they can currently compare against."""
        with self._connect() as conn:
            own = conn.execute(
                """SELECT
                       SUM(CASE WHEN visibility = 'shared' THEN 1 ELSE 0 END) AS shared,
                       SUM(CASE WHEN visibility = 'private' THEN 1 ELSE 0 END) AS private
                   FROM sessions
                   WHERE driver_profile_id = ? AND attribution_status = 'confirmed'""",
                (driver_profile_id,),
            ).fetchone()
            available = conn.execute(
                f"""SELECT COUNT(*) AS n FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL} AND p.id != ?""",
                (driver_profile_id,),
            ).fetchone()
        return {
            "shared": int(own["shared"] or 0) if own else 0,
            "private": int(own["private"] or 0) if own else 0,
            "available_from_others": int(available["n"] or 0) if available else 0,
        }

    def driver_rankings(self, driver_profile_id: int) -> pd.DataFrame:
        """Where this driver currently sits on each track's board they
        qualify for. Empty when they've shared nothing."""
        tracks = self.leaderboard_tracks()
        rows = []
        for track in tracks:
            board = self.leaderboard(track, limit=1000)
            if board.empty:
                continue
            mine = board[board["driver_profile_id"] == driver_profile_id]
            if mine.empty:
                continue
            rows.append(
                {
                    "track_name": track,
                    "rank": int(mine.iloc[0]["rank"]),
                    "field_size": len(board),
                    "best_lap_s": float(mine.iloc[0]["best_lap_s"]),
                }
            )
        return pd.DataFrame(rows)

    def leaderboard_tracks(self) -> list[str]:
        """Tracks that have at least one leaderboard-eligible session --
        so the picker never offers a track whose board would be empty."""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT s.track_name FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL} AND s.track_name IS NOT NULL
                    ORDER BY s.track_name"""
            ).fetchall()
        return [r[0] for r in rows]


class SupabaseAccountLibrary:
    """Postgres/Supabase-backed sibling of `AccountLibrary`, same public
    interface, connecting via `telemetry.db`. See
    `storage.SupabaseSessionLibrary` for the general shape of this pattern
    and why schema creation isn't done here (the shared migration in
    `supabase/migrations/0001_init.sql` owns it instead). `PUBLIC_VISIBILITY_SQL`
    is reused unchanged -- it's plain SQL with no SQLite-specific syntax."""

    def __init__(self, _unused_db_path: str | None = None):
        pass

    # ---------------------------------------------------------------- users

    def create_user(
        self,
        email: str,
        password_hash: str | None = None,
        external_auth_id: str | None = None,
        display_name: str | None = None,
        date_of_birth: str | None = None,
        guardian_email: str | None = None,
        email_verified: bool = False,
    ) -> int:
        consent = CONSENT_PENDING if is_minor(date_of_birth) else CONSENT_NOT_REQUIRED
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO users
                   (email, external_auth_id, password_hash, email_verified, display_name,
                    date_of_birth, guardian_email, guardian_consent_status, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    normalize_email(email), external_auth_id, password_hash, bool(email_verified),
                    display_name, date_of_birth, guardian_email, consent, _now(),
                ),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id

    def register_user_with_profile(
        self,
        email: str,
        password_hash: str | None = None,
        external_auth_id: str | None = None,
        display_name: str | None = None,
        date_of_birth: str | None = None,
        guardian_email: str | None = None,
        email_verified: bool = False,
    ) -> tuple[int, int]:
        user_id = self.create_user(
            email, password_hash=password_hash, external_auth_id=external_auth_id,
            display_name=display_name, date_of_birth=date_of_birth,
            guardian_email=guardian_email, email_verified=email_verified,
        )
        profile_id = self.create_profile_for_user(user_id, display_name or normalize_email(email))
        return user_id, profile_id

    def get_user(self, user_id: int) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE email = %s", (normalize_email(email),))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_user_by_external_auth_id(self, external_auth_id: str) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE external_auth_id = %s", (external_auth_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def set_email_verified(self, user_id: int, verified: bool = True) -> None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET email_verified = %s WHERE id = %s", (bool(verified), user_id))
            conn.commit()

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
            conn.commit()

    def set_guardian_consent(self, user_id: int, status: str, guardian_email: str | None = None) -> None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            if guardian_email is not None:
                cur.execute(
                    "UPDATE users SET guardian_consent_status = %s, guardian_email = %s WHERE id = %s",
                    (status, normalize_email(guardian_email), user_id),
                )
            else:
                cur.execute("UPDATE users SET guardian_consent_status = %s WHERE id = %s", (status, user_id))
            conn.commit()

    def record_login(self, user_id: int) -> None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (_now(), user_id))
            conn.commit()

    def account_is_usable(self, user_id: int) -> tuple[bool, str | None]:
        user = self.get_user(user_id)
        if user is None:
            return False, "No such account."
        if not user["email_verified"]:
            return False, "Email address not verified yet."
        if is_minor(user["date_of_birth"]) and user["guardian_consent_status"] != CONSENT_GRANTED:
            return False, "Parent/guardian consent is required before this account can be used."
        return True, None

    # ------------------------------------------------------- driver profiles

    def create_profile_for_user(self, user_id: int, display_name: str) -> int:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO driver_profiles
                   (display_name, user_id, claim_status, created_at, claimed_at)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (display_name, user_id, CLAIM_CLAIMED, _now(), _now()),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id

    def create_unclaimed_profile(
        self, display_name: str, created_by_user_id: int | None = None, invite_email: str | None = None
    ) -> tuple[int, str | None]:
        token = secrets.token_urlsafe(32) if invite_email else None
        expires = (datetime.now(timezone.utc) + timedelta(days=CLAIM_TOKEN_TTL_DAYS)).isoformat() if token else None
        status = CLAIM_INVITED if invite_email else CLAIM_UNCLAIMED
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO driver_profiles
                   (display_name, user_id, claim_status, invite_email, claim_token,
                    claim_token_expires_at, created_by_user_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    display_name, None, status, normalize_email(invite_email) if invite_email else None,
                    token, expires, created_by_user_id, _now(),
                ),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id, token

    def get_profile(self, profile_id: int) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM driver_profiles WHERE id = %s", (profile_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_profile_for_user(self, user_id: int) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM driver_profiles WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_profiles(self, claim_status: str | None = None, name_query: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM driver_profiles WHERE 1=1"
        params: tuple = ()
        if claim_status is not None:
            query += " AND claim_status = %s"
            params += (claim_status,)
        if name_query:
            query += " AND LOWER(display_name) LIKE %s"
            params += (f"%{name_query.strip().lower()}%",)
        query += " ORDER BY display_name"
        with pgdb.connect() as conn:
            return pgdb.read_sql(conn, query, params)

    def list_registered_drivers(self, name_query: str | None = None) -> pd.DataFrame:
        return self.list_profiles(claim_status=CLAIM_CLAIMED, name_query=name_query)

    # ------------------------------------------------------------ claiming

    def get_profile_by_claim_token(self, token: str) -> dict | None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM driver_profiles WHERE claim_token = %s", (token,))
            row = cur.fetchone()
        if row is None:
            return None
        profile = dict(row)
        if profile["claim_status"] == CLAIM_CLAIMED:
            return None
        expires = profile.get("claim_token_expires_at")
        if expires is not None:
            expires_dt = expires if isinstance(expires, datetime) else datetime.fromisoformat(expires)
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            if expires_dt < datetime.now(timezone.utc):
                return None
        return profile

    def claim_profile(self, profile_id: int, user_id: int) -> None:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise KeyError(f"No driver profile {profile_id}")
        if profile["claim_status"] == CLAIM_CLAIMED:
            raise ValueError("That driver profile has already been claimed.")
        existing = self.get_profile_for_user(user_id)
        if existing is not None and existing["id"] != profile_id:
            raise ValueError(
                "This account already has a driver profile. Merging two driver identities isn't supported -- "
                "report an incorrect attribution instead so it can be sorted out manually."
            )
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE driver_profiles
                   SET user_id = %s, claim_status = %s, claimed_at = %s, claim_token = NULL,
                       claim_token_expires_at = NULL
                   WHERE id = %s""",
                (user_id, CLAIM_CLAIMED, _now(), profile_id),
            )
            conn.commit()

    def claim_profile_by_token(self, token: str, user_id: int) -> int:
        profile = self.get_profile_by_claim_token(token)
        if profile is None:
            raise ValueError("That claim link is invalid, already used, or expired.")
        self.claim_profile(int(profile["id"]), user_id)
        return int(profile["id"])

    def request_profile_claim(self, profile_id: int, user_id: int, note: str | None = None) -> int:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO profile_claim_requests (driver_profile_id, requested_by_user_id, note, created_at)
                   VALUES (%s,%s,%s,%s) RETURNING id""",
                (profile_id, user_id, note, _now()),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id

    def report_attribution(
        self, reported_by_user_id: int, session_db_id: int | None = None,
        driver_profile_id: int | None = None, reason: str | None = None,
    ) -> int:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO attribution_reports
                   (session_db_id, driver_profile_id, reported_by_user_id, reason, created_at)
                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                (session_db_id, driver_profile_id, reported_by_user_id, reason, _now()),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
            return new_id

    # --------------------------------------------------------- attribution

    def attribute_session(
        self, session_db_id: int, driver_profile_id: int, uploaded_by_user_id: int | None,
        requires_confirmation: bool = False, message: str | None = None,
    ) -> int | None:
        status = ATTRIBUTION_PENDING if requires_confirmation else ATTRIBUTION_CONFIRMED
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE sessions
                   SET driver_profile_id = %s, uploaded_by_user_id = %s, attribution_status = %s
                   WHERE id = %s""",
                (driver_profile_id, uploaded_by_user_id, status, session_db_id),
            )
            request_id = None
            if requires_confirmation:
                cur.execute(
                    """INSERT INTO attribution_requests
                       (session_db_id, target_driver_profile_id, requested_by_user_id, message, created_at)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (session_db_id, driver_profile_id, uploaded_by_user_id, message, _now()),
                )
                request_id = int(cur.fetchone()["id"])
            conn.commit()
        return request_id

    def pending_attribution_requests(self, driver_profile_id: int) -> pd.DataFrame:
        with pgdb.connect() as conn:
            return pgdb.read_sql(
                conn,
                """SELECT r.*, s.source_file, s.session_index, s.start_date, s.start_time,
                          s.track_name, s.best_lap_s, s.n_laps, u.email AS requested_by_email
                   FROM attribution_requests r
                   JOIN sessions s ON s.id = r.session_db_id
                   LEFT JOIN users u ON u.id = r.requested_by_user_id
                   WHERE r.target_driver_profile_id = %s AND r.status = 'pending'
                   ORDER BY r.created_at""",
                (driver_profile_id,),
            )

    def resolve_attribution_request(self, request_id: int, accept: bool) -> None:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM attribution_requests WHERE id = %s", (request_id,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"No attribution request {request_id}")
            if accept:
                cur.execute(
                    "UPDATE sessions SET attribution_status = %s WHERE id = %s",
                    (ATTRIBUTION_CONFIRMED, row["session_db_id"]),
                )
            else:
                cur.execute(
                    "UPDATE sessions SET driver_profile_id = NULL, attribution_status = %s WHERE id = %s",
                    (ATTRIBUTION_REJECTED, row["session_db_id"]),
                )
            cur.execute(
                "UPDATE attribution_requests SET status = %s, resolved_at = %s WHERE id = %s",
                ("accepted" if accept else "rejected", _now(), request_id),
            )
            conn.commit()

    # ---------------------------------------------------------- visibility

    def set_session_visibility(self, session_db_id: int, visibility: str) -> None:
        if visibility not in (VISIBILITY_PRIVATE, VISIBILITY_SHARED):
            raise ValueError(f"Unknown visibility {visibility!r}")
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET visibility = %s WHERE id = %s", (visibility, session_db_id))
            conn.commit()

    def session_is_publicly_visible(self, session_db_id: int) -> bool:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT 1 FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE s.id = %s AND {PUBLIC_VISIBILITY_SQL}""",
                (session_db_id,),
            )
            row = cur.fetchone()
        return row is not None

    def visible_sessions_for_user(self, user_id: int | None) -> pd.DataFrame:
        with pgdb.connect() as conn:
            return pgdb.read_sql(
                conn,
                f"""SELECT s.*, p.display_name AS driver_display_name, p.claim_status
                    FROM sessions s
                    LEFT JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE (p.user_id IS NOT NULL AND p.user_id = %s AND s.attribution_status = 'confirmed')
                       OR s.uploaded_by_user_id = %s
                       OR ({PUBLIC_VISIBILITY_SQL})
                    ORDER BY s.start_date, s.start_time""",
                (user_id, user_id),
            )

    def sessions_for_profile(self, driver_profile_id: int, include_pending: bool = False) -> pd.DataFrame:
        query = "SELECT * FROM sessions WHERE driver_profile_id = %s"
        if not include_pending:
            query += " AND attribution_status = 'confirmed'"
        query += " ORDER BY start_date, start_time"
        with pgdb.connect() as conn:
            return pgdb.read_sql(conn, query, (driver_profile_id,))

    def shareable_reference_sessions(
        self, exclude_user_id: int | None = None, track_name: str | None = None,
        driver_query: str | None = None, kart_class: str | None = None,
        track_condition: str | None = None, start_date_from: str | None = None,
        start_date_to: str | None = None,
    ) -> pd.DataFrame:
        query = f"""
            SELECT s.*, p.display_name AS driver_display_name
            FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE {PUBLIC_VISIBILITY_SQL}
        """
        params: tuple = ()
        if exclude_user_id is not None:
            query += " AND (p.user_id IS NULL OR p.user_id != %s)"
            params += (exclude_user_id,)
        if track_name:
            query += " AND s.track_name = %s"
            params += (track_name,)
        if driver_query:
            query += " AND LOWER(p.display_name) LIKE %s"
            params += (f"%{driver_query.strip().lower()}%",)
        if kart_class:
            query += " AND s.kart_class = %s"
            params += (kart_class,)
        if track_condition:
            query += " AND s.track_condition = %s"
            params += (track_condition,)
        if start_date_from:
            query += " AND s.start_date >= %s"
            params += (start_date_from,)
        if start_date_to:
            query += " AND s.start_date <= %s"
            params += (start_date_to,)
        query += " ORDER BY s.best_lap_s"
        with pgdb.connect() as conn:
            return pgdb.read_sql(conn, query, params)

    # -------------------------------------------------------- leaderboards

    def leaderboard(
        self, track_name: str, track_condition: str | None = None, kart_class: str | None = None,
        limit: int = 50,
    ) -> pd.DataFrame:
        query = f"""
            SELECT p.id AS driver_profile_id, p.display_name AS driver_display_name,
                   MIN(s.best_lap_s) AS best_lap_s, s.track_name,
                   COUNT(*) AS qualifying_sessions
            FROM sessions s
            JOIN driver_profiles p ON p.id = s.driver_profile_id
            WHERE {PUBLIC_VISIBILITY_SQL}
              AND s.track_name = %s AND s.best_lap_s IS NOT NULL
        """
        params: tuple = (track_name,)
        if track_condition:
            query += " AND s.track_condition = %s"
            params += (track_condition,)
        if kart_class:
            query += " AND s.kart_class = %s"
            params += (kart_class,)
        query += " GROUP BY p.id, p.display_name, s.track_name ORDER BY best_lap_s LIMIT %s"
        params += (limit,)
        with pgdb.connect() as conn:
            board = pgdb.read_sql(conn, query, params)
        if not board.empty:
            board.insert(0, "rank", range(1, len(board) + 1))
        return board

    def community_stats(self) -> dict:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT COUNT(*) AS shared_sessions,
                           COUNT(DISTINCT p.id) AS drivers,
                           COUNT(DISTINCT s.track_name) AS tracks
                    FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL}"""
            )
            row = cur.fetchone()
        return dict(row) if row else {"shared_sessions": 0, "drivers": 0, "tracks": 0}

    def driver_contribution(self, driver_profile_id: int) -> dict:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT
                       SUM(CASE WHEN visibility = 'shared' THEN 1 ELSE 0 END) AS shared,
                       SUM(CASE WHEN visibility = 'private' THEN 1 ELSE 0 END) AS private
                   FROM sessions
                   WHERE driver_profile_id = %s AND attribution_status = 'confirmed'""",
                (driver_profile_id,),
            )
            own = cur.fetchone()
            cur.execute(
                f"""SELECT COUNT(*) AS n FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL} AND p.id != %s""",
                (driver_profile_id,),
            )
            available = cur.fetchone()
        return {
            "shared": int(own["shared"] or 0) if own else 0,
            "private": int(own["private"] or 0) if own else 0,
            "available_from_others": int(available["n"] or 0) if available else 0,
        }

    def driver_rankings(self, driver_profile_id: int) -> pd.DataFrame:
        tracks = self.leaderboard_tracks()
        rows = []
        for track in tracks:
            board = self.leaderboard(track, limit=1000)
            if board.empty:
                continue
            mine = board[board["driver_profile_id"] == driver_profile_id]
            if mine.empty:
                continue
            rows.append(
                {
                    "track_name": track,
                    "rank": int(mine.iloc[0]["rank"]),
                    "field_size": len(board),
                    "best_lap_s": float(mine.iloc[0]["best_lap_s"]),
                }
            )
        return pd.DataFrame(rows)

    def leaderboard_tracks(self) -> list[str]:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT DISTINCT s.track_name FROM sessions s
                    JOIN driver_profiles p ON p.id = s.driver_profile_id
                    WHERE {PUBLIC_VISIBILITY_SQL} AND s.track_name IS NOT NULL
                    ORDER BY s.track_name"""
            )
            rows = cur.fetchall()
        return [r["track_name"] for r in rows]


def account_library_from_env(sqlite_path: str) -> AccountLibrary | SupabaseAccountLibrary:
    """Postgres/Supabase-backed library when `SUPABASE_DB_URL`/`DATABASE_URL`
    is configured, the local SQLite one otherwise -- see
    `storage.session_library_from_env`, which makes the same choice for the
    telemetry/session store these accounts share a database with."""
    if pgdb.has_postgres_configured():
        return SupabaseAccountLibrary()
    return AccountLibrary(sqlite_path)
