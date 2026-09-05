# unigo_sync

A tool to pull session data off a Unipro UniGo laptimer automatically (over
the device's own WiFi access point) and feed it into this repo's analysis
pipeline, so returning from a track day means the data is already there --
no manual export/copy step.

## Status: Part 1 only

Nothing about the UniGo device's actual HTTP endpoints, IP, auth, or file
format is confirmed yet -- all of that is inferred from general knowledge
of similar embedded devices, not from a real capture. Building the actual
sync logic (Part 2) against guessed endpoints would mean rewriting it once
real facts come in anyway, so this repo currently contains **only the
discovery harness** used to find those facts:

- [`discovery/`](discovery/) -- capture-and-inspect tooling. See
  [`discovery/README.md`](discovery/README.md) for how to run it at the
  track/garage.
- [`findings.md`](findings.md) -- template for recording what the capture
  reveals. Empty/placeholder until someone runs the harness against a real
  device.

**Part 2 (the actual sync tool) has not been started**, and shouldn't be,
until `findings.md` is filled in from a real capture. See "The plan" below
for what it will look like once that happens.

## The plan

1. **Part 1 (done): discovery harness.** Capture real HTTP traffic between
   a browser and the UniGo device's web UI (via mitmproxy or a DevTools
   HAR export), then use `discovery/analyze_har.py` to turn that into a
   readable report: what's the device's IP, is there a session-listing
   endpoint and what does it return, what's the download URL pattern, and
   what format is the raw downloaded file actually in (TSV, like the
   Analyser software exports, or something else needing conversion).

2. **Part 2 (not started): automated sync tool.** Once `findings.md` has
   real answers, build:
   - A **portable core** (plain `requests`/stdlib Python, no
     Windows-specific code): list sessions on the device, download new
     ones, convert to TSV if the raw format isn't already TSV, write into
     wherever this repo's ingestion step reads from (see "Integration
     with the existing pipeline" below), log every attempt, retry on
     dropped WiFi/unresponsive device.
   - A **thin Windows platform layer** on top of that core: check
     `netsh wlan show interfaces` to confirm the UniGo WiFi is actually
     associated before syncing, a manual "sync now" trigger (and
     optionally a background watcher that fires when the `unigo-*`
     network is detected), wrapped in a system tray app (`pystray`) or
     simple GUI, packaged with PyInstaller into a double-clickable `.exe`.
   - A **config file** holding the discovered IP/endpoint patterns
     (separate from the logic), so a firmware update that changes an
     endpoint is a one-line config edit, not a code change.

   The portable-core/platform-layer split exists specifically so the
   future iOS version (below) only has to rewrite the platform layer --
   the protocol knowledge captured in `findings.md` and the config file
   carries over directly.

3. **Fallback, if Part 1 hits a dead end:** if the device's web UI turns
   out to have no usable HTTP interface, or the download format is a
   genuinely opaque binary with no path to TSV, fall back to scripted UI
   automation of the official Unipro Analyser desktop software (driving
   its clicks/keystrokes). More fragile and requires Analyser
   installed/running, so it's a documented Plan B, not the starting point.

4. **Future: iOS.** Out of scope for now, but designed for: iOS can't run
   an arbitrary background Python script, so this would be a native
   Swift/SwiftUI app -- a genuine second build, not a port of the Windows
   one. The one piece worth noting now: iOS restricts reading the current
   WiFi SSID directly, but `NEHotspotConfiguration` supports joining/
   preferring a network by SSID *prefix*, which maps directly onto the
   `unigo-*` naming pattern (no need to know the exact suffix in advance).
   Once joined, plain `URLSession` calls to the device's local IP work the
   same way `requests` does today. Worth testing at that point: iOS's
   "Wi-Fi Assist" may prefer cellular over a WiFi network that looks like
   it has no internet -- exactly what the kart's local-only AP looks like
   -- so this should be verified rather than assumed to be a non-issue.

## Integration with the existing pipeline

`unigo_sync`'s only job is getting session files onto disk in whatever
format the existing ingestion step expects -- it does not duplicate any
parsing or analysis logic (that stays in `telemetry/`).

One thing worth flagging explicitly: **there is currently no
watched-folder auto-ingest in this repo.** Ingestion today is either
through the Streamlit app's file uploader or by running
`scripts/ingest.py <files...>` with explicit file-path arguments -- there
is no daemon watching a directory for new files. This is a real gap
between the original ask (data should "already be there" after a sync)
and what exists today, not something to build around silently. Part 2
will need to pick one of:
- add a lightweight watched-folder poller that calls the same loading
  code `scripts/ingest.py` uses, or
- have the sync tool call `scripts/ingest.py`'s loading logic directly
  once a new session file lands, rather than just dropping a file and
  hoping something else picks it up.

Either way, if the raw UniGo download format isn't already TSV, the
conversion step lives in `unigo_sync` (it's device-specific), and its
output must match the exact TSV shape `telemetry/parser.py` expects
(see `COLUMNS` in that file, also mirrored in
`discovery/analyze_har.py`'s `KNOWN_TSV_COLUMNS`).

## Directory layout

```
unigo_sync/
  README.md          <- this file
  findings.md         <- living doc: fill in from a real device capture
  discovery/           <- Part 1: capture-and-inspect harness
    README.md          <- step-by-step track-side instructions
    analyze_har.py      <- parses a HAR (from either capture method) into a report
    mitm_capture.py      <- mitmproxy addon: live capture -> HAR
    requirements.txt      <- mitmproxy (only needed for discovery, not Part 2)
    tests/
```

## If a firmware update breaks sync (once Part 2 exists)

Re-run the discovery harness (`discovery/README.md`) against the updated
device, diff the new report against `findings.md`, and update the config
file Part 2 reads endpoints from. The goal of the config-driven design is
that this is a config edit, not a code change -- if it turns out not to
be, that's a sign the config file's shape needs to grow to cover whatever
changed.
