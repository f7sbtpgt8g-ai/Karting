"""Fetching a raw upload out of Supabase Storage.

Behind a tiny interface with a local-filesystem implementation alongside the
real one, so `tests/test_worker.py` can exercise the whole claim → download →
parse → persist → mark-complete loop without a Supabase project or a network.
That matters more than usual here: the failure paths (a missing object, a
corrupt file) are the ones worth testing, and they are exactly the ones that
are awkward to provoke against a real bucket.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Protocol


class ObjectStore(Protocol):
    """Read-only access to uploaded files, keyed by the `storage_path`
    recorded on an `upload_batches` row."""

    def download(self, path: str) -> bytes: ...


class ObjectNotFound(RuntimeError):
    """The batch references an object that isn't there. Terminal for that
    batch -- retrying cannot help, so the worker fails it with a clear
    message rather than looping."""


class SupabaseStorage:
    """Supabase Storage over its REST API, authenticated with the service
    role key. The worker is the only thing that holds that key; it never
    reaches the Next.js client bundle."""

    def __init__(self, url: str, service_key: str, bucket: str = "telemetry", timeout_s: float = 120.0):
        self.url = url.rstrip("/")
        self.service_key = service_key
        self.bucket = bucket
        self.timeout_s = timeout_s

    def download(self, path: str) -> bytes:
        request = urllib.request.Request(
            f"{self.url}/storage/v1/object/{self.bucket}/{path.lstrip('/')}",
            headers={
                "apikey": self.service_key,
                "Authorization": f"Bearer {self.service_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise ObjectNotFound(f"No object at {self.bucket}/{path}") from exc
            raise

    def upload(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Write an object, replacing any existing one at that path.

        Used to archive a session's Parquet out of the database before its
        BYTEA blob is cleared (scripts/backfill_analysis.py). Upsert rather
        than create, so re-running an interrupted archive is safe.
        """
        request = urllib.request.Request(
            f"{self.url}/storage/v1/object/{self.bucket}/{path.lstrip('/')}",
            data=data,
            method="POST",
            headers={
                "apikey": self.service_key,
                "Authorization": f"Bearer {self.service_key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s):
            return

    @classmethod
    def from_env(cls) -> "SupabaseStorage":
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY")
        if not url or not key:
            raise RuntimeError(
                "The worker needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY "
                "(or SUPABASE_SECRET_KEY) to read uploads from Storage."
            )
        return cls(url, key, bucket=os.environ.get("SUPABASE_STORAGE_BUCKET", "telemetry"))


class LocalDirectoryStore:
    """An `ObjectStore` backed by a directory on disk -- used by the tests,
    and usable for a fully offline deployment where uploads are dropped into
    a watched folder rather than pushed to Storage."""

    def __init__(self, root: str):
        self.root = root

    def download(self, path: str) -> bytes:
        full = os.path.join(self.root, path.lstrip("/"))
        if not os.path.exists(full):
            raise ObjectNotFound(f"No file at {full}")
        with open(full, "rb") as handle:
            return handle.read()

    def upload(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        full = os.path.join(self.root, path.lstrip("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(data)
