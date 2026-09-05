# UniGo discovery harness

This is Part 1 of the UniGo auto-sync project: a capture-and-inspect
toolkit for figuring out what the UniGo laptimer's own web server actually
does, so a real sync tool (Part 2) can be built against confirmed facts
instead of guesses. Nothing here talks to a UniGo device automatically --
you drive the device's web UI by hand while one of the two methods below
records the HTTP traffic, then `analyze_har.py` turns that recording into a
readable report.

Everything about the device's endpoints, IP, and file formats is currently
unknown. Run this at the track/garage with the device's WiFi AP available,
then fill in `../findings.md` with what you actually see (use
`--draft-findings` to get a starting point -- see below).

## Which capture method to use

**Prefer mitmproxy** (`mitm_capture.py`) unless you don't want to install
anything. It captures full response bodies regardless of size, gives you a
live one-line-per-request feed while you click around, and needs no manual
export step.

**Browser DevTools HAR export** is the zero-install fallback -- useful if
you can't install mitmproxy on the machine you'll have at the track. Its
main limitation: Chrome/Firefox DevTools do **not** capture the response
body for very large responses (large file downloads are the most likely
thing to hit this on a laptimer). If a session download turns out to be a
big binary file, its body may show up as "missing" in the report even
though the request itself was captured -- `analyze_har.py` will call this
out explicitly rather than fail silently. If that happens, redo the capture
with mitmproxy instead.

## Method 1: mitmproxy (recommended)

1. Install mitmproxy (only needed on the discovery machine, not for the
   eventual sync tool):
   ```
   pip install -r requirements.txt
   ```
2. Connect your laptop (or phone) to the UniGo device's WiFi AP
   (`unigo-xxxx`).
3. Start the capture addon. By default it listens on `127.0.0.1:8080` and
   writes `capture.har` in the current directory when you stop it:
   ```
   mitmdump -s mitm_capture.py --set har_out=capture.har
   ```
   Keep this terminal open -- you'll see a live one-line summary
   (`METHOD URL -> STATUS MIME (size)`) for every request as you browse.
4. Point your browser's (or OS's) HTTP proxy at `127.0.0.1:8080`. This is
   the standard "manual proxy" setting in your OS/browser network settings
   -- no VPN or special app needed, since you're proxying your own
   requests to the UniGo device, not the reverse.
   - If the UniGo UI is served over HTTPS, you'll also need to install
     mitmproxy's CA certificate once: with the proxy active, visit
     `http://mitm.it` in the browser and follow the platform-specific
     install instructions there. (If the UniGo UI is plain HTTP, as is
     typical for small embedded devices, you can skip this.)
5. In the browser, go to the UniGo device's web UI (commonly something
   like `http://192.168.4.1/` for an embedded AP, but confirm the actual
   IP from your OS's network settings once connected -- don't assume).
   Do, in order, at least:
   - Load the home page / dashboard.
   - Open whatever view lists recorded sessions.
   - Download (or "export") one specific session.
   - If there's a "delete" or "settings" page, it's fine to view those too
     (more data points), but don't actually delete anything.
6. Stop mitmdump (Ctrl+C in its terminal). It writes `capture.har` on
   exit -- confirm the file exists and is non-empty.
7. Analyze it:
   ```
   python analyze_har.py capture.har --draft-findings ../findings_draft.md
   ```
   Review the printed report (see "Reading the report" below), then fold
   the useful parts of `findings_draft.md` into `../findings.md`.

## Method 2: browser DevTools HAR export (no install required)

1. Connect to the UniGo device's WiFi AP.
2. Open the browser's DevTools (F12 or right-click -> Inspect) and switch
   to the **Network** tab *before* navigating to the device.
3. Make sure "Preserve log" is checked, so navigating between pages
   doesn't clear earlier requests.
4. Go to the device's web UI and do the same walkthrough as step 5 above
   (dashboard, session list, download one session).
5. Right-click anywhere in the Network request list -> **Save all as
   HAR with content** (the "with content" part matters -- a plain HAR
   export can omit response bodies entirely).
6. Analyze it the same way:
   ```
   python analyze_har.py capture.har --draft-findings ../findings_draft.md
   ```

## Reading the report

`analyze_har.py` prints one block per captured request: method, URL,
status, content-type, selected headers, and (for anything flagged
"interesting", any non-GET request, or any error response) a preview of
the response body. A few things it does specifically to help here:

- **It checks body content, not just the declared Content-Type.** If a
  response's body actually matches several of the real Unipro TSV column
  headers, it prints a `*** MATCHES N/28 KNOWN UNIPRO TSV COLUMN NAMES
  ***` banner regardless of what MIME type the device claimed -- this is
  how you'll spot the session-download endpoint even if the device serves
  it as `application/octet-stream` or something equally uninformative.
- **It flags requests carrying `Authorization` or `Cookie` headers**, so
  you can tell at a glance whether the device's local AP interface is
  actually unauthenticated (don't assume -- verify).
- **It never hides data**, only de-emphasizes: `--only-interesting` filters
  the printed list but the summary header always states how many requests
  were captured in total, and un-flagged requests are just as available by
  omitting that flag.
- Response bodies are truncated to 2048 characters by default; pass
  `--full-body` to see everything (useful once you've identified which
  request actually matters).
- If a body shows as "no body captured", that's the DevTools
  large-response limitation described above -- switch to mitmproxy for
  that specific request.

Useful invocations:
```
# Everything, full detail
python analyze_har.py capture.har --full-body

# Just the requests that look like they matter
python analyze_har.py capture.har --only-interesting

# Generate a findings-doc starting point
python analyze_har.py capture.har --draft-findings ../findings_draft.md
```

## Running the tests

```
cd /path/to/Karting
python -m pytest unigo_sync/discovery/tests -q
```
These test `analyze_har.py`'s parsing/classification logic against
synthetic HAR fixtures (there's no real UniGo capture available outside
the track). They don't need mitmproxy installed.
