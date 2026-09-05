"""Tests for analyze_har.py against a hand-built synthetic HAR fixture --
there's no real UniGo capture available in this environment (the device has
to be physically present), so correctness here is about the parsing/
classification logic handling the range of shapes a real capture could take,
not about matching real endpoints.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze_har  # noqa: E402


TSV_HEADER_TEXT = (
    '"Start Date"\t"Start Time"\t"Lap Number"\t"Session Time"\t"Lap Time"\t"Heading"\t'
    '"Steering Angle"\t"Vertical Acceleration"\t"RPM"\t"Steering Rate"\t"GPS Speed"\t"Slip"\n'
    "16-08-2026\t11:15:33\t0\t0\t0\t180.2\t0.1\t0.02\t0\t0.0\t0.0\t0.0\n"
)


def _har_entry(
    method="GET", url="http://192.168.4.1/", status=200, mime_type="text/html",
    body_text=None, body_base64=None, req_headers=None, resp_headers=None, omit_body=False,
):
    content = {"mimeType": mime_type}
    if not omit_body:
        if body_base64 is not None:
            content["text"] = base64.b64encode(body_base64).decode("ascii")
            content["encoding"] = "base64"
            content["size"] = len(body_base64)
        elif body_text is not None:
            content["text"] = body_text
            content["size"] = len(body_text)
        else:
            content["text"] = ""
            content["size"] = 0
    else:
        content["size"] = 999999

    return {
        "startedDateTime": "2026-08-16T11:15:00.000Z",
        "request": {
            "method": method, "url": url,
            "headers": [{"name": k, "value": v} for k, v in (req_headers or {}).items()],
        },
        "response": {
            "status": status,
            "headers": [{"name": k, "value": v} for k, v in (resp_headers or {}).items()],
            "content": content,
        },
    }


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    har = {"log": {"version": "1.2", "entries": entries}}
    path = tmp_path / "capture.har"
    path.write_text(json.dumps(har), encoding="utf-8")
    return path


@pytest.fixture
def sample_entries():
    return [
        _har_entry(url="http://192.168.4.1/style.css", mime_type="text/css", body_text="body{margin:0}"),
        _har_entry(
            url="http://192.168.4.1/api/sessions", mime_type="application/json",
            body_text=json.dumps([{"id": 1, "name": "Session A", "size": 12345}]),
        ),
        _har_entry(
            url="http://192.168.4.1/download?session=1", mime_type="application/octet-stream",
            body_base64=TSV_HEADER_TEXT.encode("utf-8"),
        ),
        _har_entry(
            url="http://192.168.4.1/download?session=2", mime_type="application/octet-stream",
            body_base64=bytes(range(256)) * 4,
        ),
        _har_entry(
            url="http://192.168.4.1/big-log", mime_type="application/octet-stream", omit_body=True,
        ),
        _har_entry(
            url="http://192.168.4.1/admin", req_headers={"Authorization": "Basic abcdef"},
            body_text="secret area",
        ),
    ]


def test_parse_har_reads_all_entries(tmp_path, sample_entries):
    har_path = _write_har(tmp_path, sample_entries)
    entries = analyze_har.parse_har(har_path)
    assert len(entries) == len(sample_entries)
    assert entries[0].url.endswith("style.css")


def test_asset_url_not_flagged_interesting(tmp_path, sample_entries):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    css_entry = next(e for e in entries if e.url.endswith(".css"))
    assert css_entry.interesting is False


def test_json_listing_endpoint_flagged_interesting(tmp_path, sample_entries):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    listing = next(e for e in entries if "sessions" in e.url)
    assert listing.interesting is True
    assert listing.mime_type == "application/json"


def test_base64_body_decoded_correctly(tmp_path, sample_entries):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    download = next(e for e in entries if "session=1" in e.url)
    assert download.body_bytes == TSV_HEADER_TEXT.encode("utf-8")
    assert download.body_is_base64_in_har is True


def test_tsv_header_detection_on_octet_stream_body(tmp_path, sample_entries):
    """The whole point of checking body *content* rather than trusting
    Content-Type: a device could serve TSV as application/octet-stream and
    the mime type alone would never reveal that."""
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    download = next(e for e in entries if "session=1" in e.url)
    assert download.tsv_header_match_count >= 5

    other_download = next(e for e in entries if "session=2" in e.url)
    assert other_download.tsv_header_match_count == 0


def test_missing_body_reports_reason_not_crash(tmp_path, sample_entries):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    big_log = next(e for e in entries if "big-log" in e.url)
    assert big_log.body_bytes is None
    assert "no body captured" in big_log.body_missing_reason


def test_print_report_runs_without_error(tmp_path, sample_entries, capsys):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    analyze_har.print_report(entries, full_body=False, only_interesting=False)
    out = capsys.readouterr().out
    assert "192.168.4.1" in out
    assert "MATCHES" in out  # the TSV-header match banner for session=1's body
    assert "no body captured" in out  # the missing-body case


def test_print_report_only_interesting_hides_assets(tmp_path, sample_entries, capsys):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    analyze_har.print_report(entries, full_body=False, only_interesting=True)
    out = capsys.readouterr().out
    assert "style.css" not in out
    assert "api/sessions" in out


def test_full_body_flag_avoids_truncation(tmp_path):
    long_json = json.dumps([{"id": i, "name": f"Session {i}"} for i in range(500)])
    entries_raw = [_har_entry(url="http://192.168.4.1/api/sessions", mime_type="application/json", body_text=long_json)]
    entries = analyze_har.parse_har(_write_har(tmp_path, entries_raw))

    truncated = analyze_har._format_body(entries[0], full_body=False)
    full = analyze_har._format_body(entries[0], full_body=True)
    assert "truncated" in truncated
    assert "truncated" not in full
    assert "Session 499" in full
    assert "Session 499" not in truncated


def test_auth_header_flagged(tmp_path, sample_entries, capsys):
    entries = analyze_har.parse_har(_write_har(tmp_path, sample_entries))
    analyze_har.print_report(entries, full_body=False, only_interesting=False)
    out = capsys.readouterr().out
    assert "auth-looking header" in out


def test_write_draft_findings_lists_hosts_and_flagged_entries(tmp_path, sample_entries):
    har_path = _write_har(tmp_path, sample_entries)
    entries = analyze_har.parse_har(har_path)
    out_path = tmp_path / "draft.md"
    analyze_har.write_draft_findings(entries, out_path, har_path)

    text = out_path.read_text(encoding="utf-8")
    assert "192.168.4.1" in text
    assert "api/sessions" in text
    assert "style.css" not in text  # not flagged, shouldn't appear in the draft


def test_main_end_to_end(tmp_path, sample_entries, capsys):
    har_path = _write_har(tmp_path, sample_entries)
    draft_path = tmp_path / "draft.md"
    exit_code = analyze_har.main([str(har_path), "--draft-findings", str(draft_path)])
    assert exit_code == 0
    assert draft_path.exists()
    out = capsys.readouterr().out
    assert "Draft findings written" in out


def test_main_handles_missing_file(tmp_path, capsys):
    exit_code = analyze_har.main([str(tmp_path / "nope.har")])
    assert exit_code == 1


def test_main_handles_invalid_har(tmp_path, capsys):
    bad = tmp_path / "bad.har"
    bad.write_text("not json", encoding="utf-8")
    exit_code = analyze_har.main([str(bad)])
    assert exit_code == 1


def test_empty_har_does_not_crash(tmp_path, capsys):
    har_path = _write_har(tmp_path, [])
    exit_code = analyze_har.main([str(har_path)])
    assert exit_code == 0
    assert "No requests found" in capsys.readouterr().out
