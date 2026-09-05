"""mitmproxy addon: capture UniGo device HTTP traffic and print a live
one-line summary of each request as it happens, then write a standard HAR
file on exit so `analyze_har.py` can process it exactly like a browser
DevTools export.

Usage (see ../README.md for full track-side instructions, including proxy
setup on the capturing device and installing mitmproxy's CA cert):

    mitmdump -s mitm_capture.py --set har_out=capture.har

Then point your laptop/phone's HTTP proxy at this machine (mitmdump listens
on 127.0.0.1:8080 by default), install mitmproxy's CA cert if the UniGo UI
is served over HTTPS, and browse the device's web UI: view the session
list, download a session, etc. Every request is logged to the terminal as
it's captured. Press Ctrl+C to stop -- the HAR is written on shutdown.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mitmproxy import ctx, http

DEFAULT_HAR_OUT = "capture.har"


def _headers_to_har(headers) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in headers.items(multi=True)]


def _content_to_har(data: bytes | None, mime_type: str) -> dict[str, Any]:
    if not data:
        return {"mimeType": mime_type, "text": "", "size": 0}
    try:
        text = data.decode("utf-8")
        return {"mimeType": mime_type, "text": text, "size": len(data)}
    except UnicodeDecodeError:
        return {
            "mimeType": mime_type,
            "text": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
            "size": len(data),
        }


def _flow_to_har_entry(flow: http.HTTPFlow) -> dict[str, Any]:
    request = flow.request
    response = flow.response

    mime_type = response.headers.get("content-type", "") if response else ""
    body = response.content if response else None

    return {
        "startedDateTime": datetime.fromtimestamp(
            request.timestamp_start, tz=timezone.utc
        ).isoformat(),
        "request": {
            "method": request.method,
            "url": request.pretty_url,
            "headers": _headers_to_har(request.headers),
        },
        "response": {
            "status": response.status_code if response else 0,
            "headers": _headers_to_har(response.headers) if response else [],
            "content": _content_to_har(body, mime_type),
        },
    }


class HarCapture:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def load(self, loader) -> None:
        loader.add_option(
            name="har_out",
            typespec=str,
            default=DEFAULT_HAR_OUT,
            help="Path to write the HAR capture to when mitmdump exits.",
        )

    def response(self, flow: http.HTTPFlow) -> None:
        entry = _flow_to_har_entry(flow)
        self.entries.append(entry)

        status = entry["response"]["status"]
        mime = entry["response"]["content"].get("mimeType", "") or "-"
        size = entry["response"]["content"].get("size", 0)
        ctx.log.info(
            f"[{len(self.entries):>3}] {flow.request.method:6} {flow.request.pretty_url} "
            f"-> {status} {mime} ({size} bytes)"
        )

    def done(self) -> None:
        out_path = Path(ctx.options.har_out)
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "unigo_sync mitm_capture", "version": "1.0"},
                "entries": self.entries,
            }
        }
        out_path.write_text(json.dumps(har, indent=2), encoding="utf-8")
        ctx.log.info(f"Wrote {len(self.entries)} entries to {out_path}")


addons = [HarCapture()]
