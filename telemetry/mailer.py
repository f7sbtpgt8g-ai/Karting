"""Outbound email: message templates and pluggable delivery.

Delivery is behind a `EmailSender` interface so the app never calls an SMTP
library directly:

- `OutboxEmailSender` (default) records messages to an `email_outbox` table
  and sends nothing. This is what runs locally and in tests, and it makes
  every template inspectable without a mail server.
- `SmtpEmailSender` actually sends, for a real deployment.

**Invite emails are gated off by default.** `INVITE_EMAILS_ENABLED` (env:
`KARTING_ENABLE_INVITE_EMAILS`) must be explicitly turned on before a claim
invite is delivered to anyone. Every other message here goes to somebody who
asked for it -- they registered, or they requested a password reset -- but a
claim invite is unsolicited contact with a person who has not consented to
being on the platform at all, and in this sport a meaningful share of them
are minors. Recording the invite in the outbox without sending it keeps the
whole flow testable and reviewable while that copy gets the legal/privacy
read it needs; flipping the flag is a deliberate, informed decision rather
than something that happens by default on first deploy.
"""

from __future__ import annotations

import os
import smtplib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage

from . import db as pgdb

MAILER_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT,
    sent INTEGER NOT NULL DEFAULT 0,
    suppressed_reason TEXT,
    created_at TEXT NOT NULL
);
"""


def invite_emails_enabled() -> bool:
    return os.environ.get("KARTING_ENABLE_INVITE_EMAILS", "").strip().lower() in ("1", "true", "yes")


@dataclass
class Email:
    to_email: str
    subject: str
    body: str
    kind: str


# ---------------------------------------------------------------- templates


def verification_email(to_email: str, verify_link: str) -> Email:
    return Email(
        to_email=to_email,
        subject="Confirm your email address",
        kind="verify_email",
        body=(
            "Thanks for creating a karting telemetry account.\n\n"
            f"Confirm your email address to finish setting it up:\n{verify_link}\n\n"
            "This link expires in 48 hours. If you didn't create an account, you can ignore this email."
        ),
    )


def password_reset_email(to_email: str, reset_link: str) -> Email:
    return Email(
        to_email=to_email,
        subject="Reset your password",
        kind="password_reset",
        body=(
            "Someone asked to reset the password for this email address.\n\n"
            f"Reset it here:\n{reset_link}\n\n"
            "This link expires in 2 hours and can only be used once. "
            "If this wasn't you, you can ignore this email -- your password won't change."
        ),
    )


def guardian_consent_email(to_email: str, driver_name: str, consent_link: str) -> Email:
    return Email(
        to_email=to_email,
        subject=f"Permission needed for {driver_name}'s karting account",
        kind="guardian_consent",
        body=(
            f"{driver_name} has signed up for a karting telemetry account and given this address as a "
            "parent or guardian's.\n\n"
            "Because they're under 16, the account stays inactive until you approve it:\n"
            f"{consent_link}\n\n"
            "What it stores: lap timing data from their kart's logger -- lap times, speed and GPS traces "
            "of the track.\n\n"
            "What other people can see: uploaded sessions are shared by default. That means their lap "
            "times appear on that track's leaderboard under their driver name, and other drivers can "
            "compare their own laps against them. No contact details are ever shown. Either of you can "
            "switch any session back to private at any time, with one toggle, and it disappears from "
            "those leaderboards immediately.\n\n"
            "If you'd rather nothing was shared, approve the account and switch sharing off from the "
            "'My sessions & sharing' page -- or simply ignore this email, and the account stays inactive."
        ),
    )


def claim_invite_email(to_email: str, driver_name: str, uploaded_by: str, sessions_summary: str, claim_link: str) -> Email:
    """Copy for the unsolicited invite. Deliberately flat: it describes what
    already exists and makes declining as easy as accepting, rather than
    selling a signup. See the module docstring for why sending is gated."""
    return Email(
        to_email=to_email,
        subject=f"Karting session data has been added under your name by {uploaded_by}",
        kind="claim_invite",
        body=(
            f"Hello {driver_name},\n\n"
            f"{uploaded_by} uploaded karting session data and recorded it as yours:\n\n"
            f"{sessions_summary}\n\n"
            "Right now this data is private -- nobody else can see it, and it isn't on any leaderboard, "
            "because it hasn't been confirmed as yours.\n\n"
            "If this is you and you'd like access to it, you can set up an account here:\n"
            f"{claim_link}\n\n"
            "One thing to know before you do: once it's yours, sessions are shared by default, so your lap "
            "times would appear on that track's leaderboard under your name. You can switch any session "
            "back to private with one toggle, at any time.\n\n"
            "If it isn't you, or you'd rather it wasn't stored, reply to this email and we'll delete it. "
            "You don't need to create an account to have it removed.\n\n"
            "If you're under 16, please check with a parent or guardian before signing up -- they'll be "
            "asked to approve the account.\n\n"
            "This link expires in 14 days. If you do nothing, the data stays private and you won't be "
            "emailed about it again."
        ),
    )


def attribution_request_email(to_email: str, requester: str, sessions_summary: str, review_link: str) -> Email:
    return Email(
        to_email=to_email,
        subject=f"{requester} wants to add a session to your history",
        kind="attribution_request",
        body=(
            f"{requester} uploaded karting session data and says it's yours:\n\n"
            f"{sessions_summary}\n\n"
            "It won't be added to your history unless you accept it:\n"
            f"{review_link}\n\n"
            "If it isn't yours, reject it and it'll stay with whoever uploaded it."
        ),
    )


def claim_notification_email(to_email: str, driver_name: str, claimed_by: str) -> Email:
    """Sent to the uploader when a placeholder they created gets claimed --
    a sanity check, not an approval gate (see `request_profile_claim`)."""
    return Email(
        to_email=to_email,
        subject=f"{claimed_by} has claimed the driver profile '{driver_name}'",
        kind="claim_notification",
        body=(
            f"The driver profile '{driver_name}' that you created has been claimed by {claimed_by}, "
            "who now has access to the sessions recorded under it.\n\n"
            "If that doesn't look right, report it from the profile's page and it'll be looked into."
        ),
    )


# ------------------------------------------------------------------ senders


class EmailSender:
    def send(self, email: Email) -> bool:
        raise NotImplementedError


class OutboxEmailSender(EmailSender):
    """Records messages instead of sending them. The default, so a local
    deployment and the test suite never need a mail server -- and so the
    exact copy that *would* have gone out is inspectable."""

    name = "outbox"

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._connect() as conn:
            conn.executescript(MAILER_SCHEMA)
            conn.commit()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def send(self, email: Email) -> bool:
        suppressed = None
        if email.kind == "claim_invite" and not invite_emails_enabled():
            suppressed = "Invite emails are disabled (KARTING_ENABLE_INVITE_EMAILS is not set)."
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO email_outbox (to_email, subject, body, kind, sent, suppressed_reason, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    email.to_email, email.subject, email.body, email.kind, 0, suppressed,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        return False

    def outbox(self, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM email_outbox"
        params: tuple = ()
        if kind:
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]


class SmtpEmailSender(EmailSender):
    """Real delivery. Still records to the outbox first, so there is a local
    record of what was sent (and of anything suppressed by the invite
    gate)."""

    name = "smtp"

    def __init__(
        self, db_path: str, host: str, port: int = 587, username: str | None = None,
        password: str | None = None, from_email: str = "noreply@example.com", use_tls: bool = True,
    ):
        self.outbox_sender = OutboxEmailSender(db_path)
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.from_email, self.use_tls = from_email, use_tls

    def send(self, email: Email) -> bool:
        if email.kind == "claim_invite" and not invite_emails_enabled():
            self.outbox_sender.send(email)  # records the suppression reason
            return False

        message = EmailMessage()
        message["Subject"] = email.subject
        message["From"] = self.from_email
        message["To"] = email.to_email
        message.set_content(email.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError):
            self.outbox_sender.send(email)
            return False

        with self.outbox_sender._connect() as conn:
            conn.execute(
                """INSERT INTO email_outbox (to_email, subject, body, kind, sent, created_at)
                   VALUES (?,?,?,?,1,?)""",
                (email.to_email, email.subject, email.body, email.kind, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        return True


class SupabaseOutboxEmailSender(EmailSender):
    """Postgres/Supabase-backed sibling of `OutboxEmailSender`, same public
    interface, connecting via `telemetry.db`. See
    `storage.SupabaseSessionLibrary` for why schema creation isn't done
    here."""

    name = "outbox"

    def __init__(self, _unused_db_path: str | None = None):
        pass

    def send(self, email: Email) -> bool:
        suppressed = None
        if email.kind == "claim_invite" and not invite_emails_enabled():
            suppressed = "Invite emails are disabled (KARTING_ENABLE_INVITE_EMAILS is not set)."
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO email_outbox (to_email, subject, body, kind, sent, suppressed_reason, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    email.to_email, email.subject, email.body, email.kind, False, suppressed,
                    datetime.now(timezone.utc),
                ),
            )
            conn.commit()
        return False

    def outbox(self, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM email_outbox"
        params: tuple = ()
        if kind:
            query += " WHERE kind = %s"
            params = (kind,)
        query += " ORDER BY created_at DESC, id DESC"
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]


class SupabaseSmtpEmailSender(EmailSender):
    """Postgres/Supabase-backed sibling of `SmtpEmailSender` -- real
    delivery, recording to the Postgres `email_outbox` table (via
    `SupabaseOutboxEmailSender`) instead of SQLite either way."""

    name = "smtp"

    def __init__(
        self, host: str, port: int = 587, username: str | None = None,
        password: str | None = None, from_email: str = "noreply@example.com", use_tls: bool = True,
    ):
        self.outbox_sender = SupabaseOutboxEmailSender()
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.from_email, self.use_tls = from_email, use_tls

    def send(self, email: Email) -> bool:
        if email.kind == "claim_invite" and not invite_emails_enabled():
            self.outbox_sender.send(email)
            return False

        message = EmailMessage()
        message["Subject"] = email.subject
        message["From"] = self.from_email
        message["To"] = email.to_email
        message.set_content(email.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError):
            self.outbox_sender.send(email)
            return False

        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO email_outbox (to_email, subject, body, kind, sent, created_at)
                   VALUES (%s,%s,%s,%s,TRUE,%s)""",
                (email.to_email, email.subject, email.body, email.kind, datetime.now(timezone.utc)),
            )
            conn.commit()
        return True


def sender_from_env(db_path: str) -> EmailSender:
    """SMTP when `SMTP_HOST` is configured, outbox otherwise -- so nothing
    is ever sent from a machine that hasn't been deliberately set up to.
    Backed by Postgres/Supabase when `SUPABASE_DB_URL`/`DATABASE_URL` is
    configured, the local SQLite outbox otherwise -- see
    `storage.session_library_from_env`, which makes the same choice for
    the telemetry/session store this shares a database with."""
    host = os.environ.get("SMTP_HOST")
    if pgdb.has_postgres_configured():
        if not host:
            return SupabaseOutboxEmailSender()
        return SupabaseSmtpEmailSender(
            host=host, port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ.get("SMTP_USERNAME"), password=os.environ.get("SMTP_PASSWORD"),
            from_email=os.environ.get("SMTP_FROM", "noreply@example.com"),
        )
    if not host:
        return OutboxEmailSender(db_path)
    return SmtpEmailSender(
        db_path, host=host, port=int(os.environ.get("SMTP_PORT", "587")),
        username=os.environ.get("SMTP_USERNAME"), password=os.environ.get("SMTP_PASSWORD"),
        from_email=os.environ.get("SMTP_FROM", "noreply@example.com"),
    )
