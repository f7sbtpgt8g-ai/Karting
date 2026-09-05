#!/usr/bin/env python
"""Parse a HAR (HTTP Archive) capture and print every request/response in
order, with enough detail to figure out what a UniGo laptimer's embedded
web interface is actually talking to.

Works on a HAR file from EITHER capture method described in
`unigo_sync/discovery/README.md`:
- A browser's DevTools Network tab, "Save all as HAR with content".
- `mitmdump -s mitm_capture.py` (see that script -- it writes a HAR file
  in this same standard format on exit).

This script does no guessing about what the device's protocol IS -- it's
purely an inspection aid. The actual endpoint decisions belong in
`unigo_sync/findings.md`, written up by a human (or Claude, on a later
turn) after looking at what this prints.

Usage:
    python analyze_har.py capture.har
    python analyze_har.py capture.har --full-body        # never truncate bodies
    python analyze_har.py capture.har --draft-findings out.md
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# The confirmed column header from a real Unipro Analyser TSV export (see
# telemetry/parser.py::COLUMNS in the main analysis tool). If a captured
# response body contains a good chunk of these column names, that's about
# as strong a signal as we can get from static inspection alone that the
# device is already handing out Analyser-format TSV directly -- versus some
# other raw/binary format the desktop software converts before export.
KNOWN_TSV_COLUMNS = [
    "Start Date", "Start Time", "Lap Number", "Session Time", "Lap Time",
    "Heading", "Steering Angle", "Vertical Acceleration", "RPM", "Steering Rate",
    "GPS Speed", "Slip", "Horizontal DOP", "Inverse Corner Radius", "Latitude",
    "GPS Distance", "GPS Lateral Acceleration", "GPS Longitudinal Acceleration",
    "Internal Temperature", "Vertical DOP", "Longitude", "Battery Voltage",
    "Positional DOP", "Time", "Temperature 1", "GPS Total Acceleration",
    "RPM unfiltered", "Altitude",
]

# Substrings in a URL path that suggest "this might be session-listing or
# session-download traffic" rather than routine UI asset loading (CSS, JS,
# icons, fonts). Deliberately broad and over-inclusive -- false positives
# here just mean an entry gets a star it didn't need; false negatives mean
# missing the one request that actually mattered, which is much worse.
INTERESTING_URL_HINTS = [
    "session", "log", "run", "lap", "race", "track", "data", "file", "download",
    "export", "list", "api", "sd", "storage", "record", "trip", "activity",
]

# File extensions in a URL path that read as routine web-UI assets, not
# data -- used only to de-emphasize obvious noise in the summary, never to
# hide anything (every request is still printed in full below).
ASSET_EXTENSIONS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff",
    ".woff2", ".ttf", ".map",
)

BINARY_MIME_HINTS = ("octet-stream", "application/x-", "binary")
TEXTLIKE_MIME_HINTS = ("json", "xml", "text/", "html", "csv", "tsv")

# Response bodies larger than this print only a summary + hex/text preview
# by default; pass --full-body to always print everything. Keeps a capture
# with one large session file from burying the rest of the report.
DEFAULT_BODY_PREVIEW_BYTES = 2048


@dataclass
class ParsedEntry:
    index: int
    started_at: str
    method: str
    url: str
    status: int
    request_headers: dict
    response_headers: dict
    mime_type: str
    body_bytes: bytes | None
    body_is_base64_in_har: bool
    body_missing_reason: str | None  # HAR sometimes omits the body entirely
    interesting: bool = False
    tsv_header_match_count: int = 0


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc
    except ValueError:
        return "?"


def _looks_interesting(url: str, mime_type: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(ASSET_EXTENSIONS):
        return False
    if any(hint in mime_type.lower() for hint in ("json", "xml", "octet-stream")):
        return True
    return any(hint in path for hint in INTERESTING_URL_HINTS)


def _decode_body(content: dict) -> tuple[bytes | None, bool, str | None]:
    """HAR's `response.content.text` is either plain text, or base64 when
    `encoding == "base64"` (the usual case for anything binary). Some
    captures omit the body altogether (`text` missing) -- DevTools does
    this for very large responses unless the export explicitly requested
    content, which is exactly the "confirm this actually captured the file
    body" pitfall called out in the README.
    """
    text = content.get("text")
    if text is None:
        size = content.get("size", -1)
        return None, False, f"no body captured (HAR reports size={size} -- see README's note on this)"
    encoding = content.get("encoding")
    if encoding == "base64":
        try:
            return base64.b64decode(text), True, None
        except (ValueError, TypeError) as exc:
            return None, True, f"declared base64 but failed to decode: {exc}"
    return text.encode("utf-8", errors="replace"), False, None


def _count_tsv_header_matches(body_text: str) -> int:
    return sum(1 for col in KNOWN_TSV_COLUMNS if col in body_text)


def _headers_dict(entries: list[dict]) -> dict:
    return {h["name"]: h["value"] for h in entries}


def parse_har(path: Path) -> list[ParsedEntry]:
    with open(path, "r", encoding="utf-8") as f:
        har = json.load(f)

    parsed = []
    for i, entry in enumerate(har.get("log", {}).get("entries", []), start=1):
        request = entry.get("request", {})
        response = entry.get("response", {})
        content = response.get("content", {})
        mime_type = content.get("mimeType", "") or ""
        body_bytes, was_base64, missing_reason = _decode_body(content)

        url = request.get("url", "")
        parsed_entry = ParsedEntry(
            index=i,
            started_at=entry.get("startedDateTime", "?"),
            method=request.get("method", "?"),
            url=url,
            status=response.get("status", 0),
            request_headers=_headers_dict(request.get("headers", [])),
            response_headers=_headers_dict(response.get("headers", [])),
            mime_type=mime_type,
            body_bytes=body_bytes,
            body_is_base64_in_har=was_base64,
            body_missing_reason=missing_reason,
        )
        parsed_entry.interesting = _looks_interesting(url, mime_type)
        if body_bytes is not None:
            try:
                parsed_entry.tsv_header_match_count = _count_tsv_header_matches(
                    body_bytes.decode("utf-8", errors="replace")
                )
            except Exception:
                pass
        parsed.append(parsed_entry)
    return parsed


def _is_probably_text(sample: bytes) -> bool:
    if not sample:
        return True
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return printable / len(sample) > 0.9


def _format_body(entry: ParsedEntry, full_body: bool) -> str:
    if entry.body_bytes is None:
        return f"    (body not available: {entry.body_missing_reason})"

    body = entry.body_bytes
    lines = []

    if entry.tsv_header_match_count >= 5:
        lines.append(
            f"    *** MATCHES {entry.tsv_header_match_count}/{len(KNOWN_TSV_COLUMNS)} KNOWN UNIPRO TSV "
            "COLUMN NAMES -- this looks like it might already be Analyser-format TSV. ***"
        )

    mime = entry.mime_type.lower()
    is_texty = any(hint in mime for hint in TEXTLIKE_MIME_HINTS) or (
        not mime and _is_probably_text(body[:512])
    )

    if is_texty:
        text = body.decode("utf-8", errors="replace")
        if "json" in mime:
            try:
                text = json.dumps(json.loads(text), indent=2)
            except (json.JSONDecodeError, ValueError):
                pass  # fall through and print as-is
        truncated = not full_body and len(text) > DEFAULT_BODY_PREVIEW_BYTES
        shown = text[:DEFAULT_BODY_PREVIEW_BYTES] if truncated else text
        lines.append("    --- body (text) ---")
        lines.extend(f"    {line}" for line in shown.splitlines())
        if truncated:
            lines.append(f"    ... truncated, {len(text) - DEFAULT_BODY_PREVIEW_BYTES} more characters (--full-body to see all)")
    else:
        preview_len = len(body) if full_body else min(len(body), DEFAULT_BODY_PREVIEW_BYTES)
        preview = body[:preview_len]
        hexdump = " ".join(f"{b:02x}" for b in preview[:256])
        looks_text = _is_probably_text(preview[:512])
        lines.append(f"    --- body (binary, {len(body)} bytes total, {'looks mostly text/ASCII' if looks_text else 'looks genuinely binary'}) ---")
        lines.append(f"    first bytes (hex): {hexdump}{' ...' if len(preview) > 256 else ''}")
        try:
            ascii_preview = preview[:256].decode("ascii", errors="replace")
            lines.append(f"    first bytes (ascii): {ascii_preview!r}")
        except Exception:
            pass
        if len(body) > preview_len:
            lines.append(f"    ... {len(body) - preview_len} more bytes not shown (--full-body to see all)")

    return "\n".join(lines)


def print_report(entries: list[ParsedEntry], full_body: bool, only_interesting: bool) -> None:
    hosts = sorted({_host(e.url) for e in entries if e.url})
    print("=" * 78)
    print(f"{len(entries)} request(s) captured across {len(hosts)} host(s):")
    for h in hosts:
        print(f"  - {h}")
    print("=" * 78)

    interesting_count = sum(1 for e in entries if e.interesting)
    print(f"\n{interesting_count} of {len(entries)} request(s) flagged as possibly interesting (marked ★ below).")
    print("Flagging is a heuristic over the URL/content-type -- everything is still listed, nothing is hidden.\n")

    for e in entries:
        if only_interesting and not e.interesting:
            continue
        star = "★ " if e.interesting else "  "
        print(f"{star}[{e.index}] {e.method} {e.url}")
        print(f"    -> {e.status}  {e.mime_type or '(no content-type)'}  @ {e.started_at}")

        auth_header = next((v for k, v in e.request_headers.items() if k.lower() in ("authorization", "cookie")), None)
        if auth_header:
            print(f"    ! request carries an auth-looking header: present (value redacted -- check manually if relevant)")

        interesting_req_headers = {
            k: v for k, v in e.request_headers.items()
            if k.lower() in ("host", "accept", "content-type", "content-length", "referer")
        }
        if interesting_req_headers:
            print(f"    request headers: {interesting_req_headers}")

        if e.method != "GET" or e.interesting or e.status >= 400:
            print(_format_body(e, full_body))
        print()

    if only_interesting:
        skipped = len(entries) - sum(1 for e in entries if e.interesting)
        print(f"({skipped} non-flagged request(s) hidden by --only-interesting; omit that flag to see everything.)")


def write_draft_findings(entries: list[ParsedEntry], out_path: Path, source_har: Path) -> None:
    """A scaffold, not a finished document -- lists what was flagged as
    interesting with a spot for notes, meant to be folded into (or replace)
    `unigo_sync/findings.md` by a human after actually reading the report."""
    hosts = sorted({_host(e.url) for e in entries if e.url})
    interesting = [e for e in entries if e.interesting]

    lines = [
        f"# Draft findings from `{source_har.name}`",
        "",
        f"Generated by `analyze_har.py` -- NOT a finished findings doc, a starting point.",
        f"Review each entry below against the full `analyze_har.py {source_har.name}` output "
        "and fold whatever's confirmed into `unigo_sync/findings.md`.",
        "",
        "## Hosts contacted",
        "",
    ]
    if hosts:
        lines.extend(f"- `{h}`" for h in hosts)
    else:
        lines.append("- (none found)")
    lines.append("")
    lines.append("## Flagged requests (heuristic -- verify each one)")
    lines.append("")
    if not interesting:
        lines.append("None flagged. Either nothing data-like was captured, or the heuristics in "
                      "`INTERESTING_URL_HINTS` need widening -- check the full report, not just this draft.")
    for e in interesting:
        lines.append(f"### `{e.method} {e.url}`")
        lines.append("")
        lines.append(f"- Status: {e.status}")
        lines.append(f"- Content-Type: {e.mime_type or '(none)'}")
        if e.tsv_header_match_count >= 5:
            lines.append(f"- **Matches {e.tsv_header_match_count}/{len(KNOWN_TSV_COLUMNS)} known Unipro TSV column names**")
        if e.body_missing_reason:
            lines.append(f"- Body not captured: {e.body_missing_reason}")
        lines.append("- Notes: _(fill in -- what is this endpoint actually for?)_")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDraft findings written to {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("har_file", type=Path, help="Path to a .har file")
    parser.add_argument("--full-body", action="store_true", help="Never truncate response bodies")
    parser.add_argument("--only-interesting", action="store_true", help="Only print requests flagged as interesting")
    parser.add_argument("--draft-findings", type=Path, default=None, metavar="OUT.md", help="Also write a draft findings scaffold to this path")
    args = parser.parse_args(argv)

    if not args.har_file.exists():
        print(f"No such file: {args.har_file}", file=sys.stderr)
        return 1

    try:
        entries = parse_har(args.har_file)
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"Could not parse {args.har_file} as HAR: {exc}", file=sys.stderr)
        print("(Is this a real HAR export? DevTools: right-click the network log -> 'Save all as HAR with content'.)", file=sys.stderr)
        return 1

    if not entries:
        print("No requests found in this capture -- was anything actually browsed while it was recording?")
        return 0

    print_report(entries, full_body=args.full_body, only_interesting=args.only_interesting)

    if args.draft_findings:
        write_draft_findings(entries, args.draft_findings, args.har_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
