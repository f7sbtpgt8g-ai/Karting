# UniGo device findings

Living document. **Everything below is a placeholder until someone runs the
discovery harness (`discovery/README.md`) against a real UniGo device** --
nothing here is confirmed yet. Fill in each section from the
`analyze_har.py` report (and its `--draft-findings` output as a starting
point) after a real capture. Keep this updated across firmware versions --
note the firmware version a finding was captured against if it's ever
relevant (i.e. if something changes between updates).

Last updated: _(never -- awaiting first real capture)_

## Device info

- **Model:** _(UniGo One / UniGo 7006 / other -- confirm)_
- **Firmware version:** _(check the device's settings/about page if there is one)_
- **AP SSID:** _(confirm exact pattern -- assumed `unigo-xxxx`, unconfirmed)_
- **Device IP once connected:** _(assumed `192.168.4.1`-style embedded-AP range -- confirm from actual OS network settings, don't assume)_
- **Auth on the local AP web interface:** _(assumed none -- VERIFY, don't assume. Check for `Authorization`/`Cookie` request headers in the capture report.)_
- **Protocol:** _(HTTP or HTTPS? If HTTPS, note that mitmproxy's CA cert had to be installed to see it.)_

## Session-listing endpoint

- **URL:** _(e.g. `GET /api/sessions` -- fill in exact path once known)_
- **Response format:** _(JSON / XML / HTML -- paste a real trimmed example below)_
- **Fields available per session:** _(ID? name? timestamp? size? Enough to detect "is this new" against a local record of already-synced IDs?)_
- **Example response:**
  ```
  (paste a real, trimmed example here)
  ```

## Session-download endpoint

- **URL pattern:** _(e.g. `GET /download?session=<id>` -- fill in once known)_
- **How to trigger it from the UI:** _(what button/link, for future reference)_

## File format findings

- **Declared Content-Type on download:** _(what the device claims -- may be misleading, see next line)_
- **Actual content (verified by inspection, not just Content-Type):** _(is it already Analyser-format TSV? Check the `analyze_har.py` "MATCHES N/28 KNOWN UNIPRO TSV COLUMN NAMES" banner. If it's something else -- binary, a different text format -- describe it here and note that Part 2 will need a conversion step.)_
- **If NOT already TSV, notes toward a conversion path:** _(byte layout, any headers/magic bytes observed, anything that suggests a known format)_

## Open questions

- _(anything the capture didn't answer, or that needs a second capture pass to confirm -- e.g. does the listing endpoint distinguish "new since last sync," does the device support range requests for partial downloads, does repeated polling of the listing endpoint cause any issues on the embedded system, etc.)_

## Raw capture references

- _(filenames/dates of the actual `.har` files this document was derived from, so a future re-check has something to diff against. Consider keeping the raw `.har` files alongside this doc, or noting where they're stored, if they contain nothing sensitive.)_
