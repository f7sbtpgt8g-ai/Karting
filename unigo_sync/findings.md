# UniGo device findings

Living document, now populated from a real capture (see "Raw capture
references" below). Firmware version at capture time: **1.20.002**
(reported two slightly different ways by the device itself -- see Device
info). Re-run the discovery harness and diff against this doc if a
firmware update changes anything.

Last updated: 2026-09-05, from a real HAR capture of the device's web UI
(page load + session list + downloading one session, three times), plus a
real Unipro Analyser TSV export of that exact same session used as ground
truth to attempt decoding the raw binary format.

## Device info

- **Model:** device identifies itself internally as `"unigo-one"` (see the
  `/file?filename=...` binary header, below) -- consistent with UniGo One.
- **Firmware version:** `1.20.002` (from the binary file header's embedded
  JSON) / `1.20.2` (from `/status`, below) -- same version, two different
  string formats depending on which endpoint reports it. Worth checking
  both if diagnosing a firmware-version-dependent issue later.
- **Serial number:** present in the binary file header (`serial_number`,
  an integer). Treat as a per-device identifier if useful later (e.g. to
  support multiple UniGo units).
- **AP SSID:** not captured -- the laptop was already connected when the
  capture started. Still unconfirmed; grab it from the OS WiFi list next
  time and note it here.
- **Device IP once connected:** confirmed **`192.168.4.1`** -- the assumed
  embedded-AP address was correct.
- **Auth on the local AP web interface:** confirmed **none**. No request
  in the capture carries an `Authorization` or `Cookie` header, and the UI
  itself has no login step -- `analyze_har.py`'s auth-header flag never
  triggered anywhere in this capture.
- **Protocol:** plain **HTTP**, not HTTPS. No CA cert install needed.

### Endpoints seen or referenced

All confirmed against `192.168.4.1`, all plain HTTP, no auth:

| Endpoint | Method | Purpose | Seen in capture? |
|---|---|---|---|
| `/` | GET | Main UI page (HTML) | yes |
| `/status` | GET | Device/firmware status, polled periodically by the UI | yes |
| `/file?filelist` | GET | List all recorded session files | yes |
| `/file?filename=<name>` | GET | Download one session file (raw binary) | yes |
| `/file?delete=<name>` | GET | **Deletes** the named file | found in page JS only, not exercised -- see warning below |
| `/update` | POST | Firmware update upload (unrelated to sync) | found in page JS only, not exercised |

**Important gotcha for Part 2:** the delete operation is a plain `GET`
request (`/file?delete=<name>`), not a `POST`/`DELETE`. A naive HTTP
client, a browser prefetcher, or any code that treats GET as safe/
idempotent could delete a session by accident. The sync tool must never
construct a URL from unsanitized/unexpected input in a way that could hit
this path, and should probably allowlist request paths it's willing to
make rather than building arbitrary query strings.

`/status` response example:
```json
{
  "status": 0,
  "version": "1.20.2",
  "compile_time": "12:10:23",
  "compile_date": "Feb 11 2026"
}
```

## Session-listing endpoint

- **URL:** `GET /file?filelist`
- **Response format:** JSON
- **Fields available per session:** `name` (string, includes the track/
  session label and driver name baked into the filename -- see naming
  convention below) and `size` (string containing an integer, bytes).
  **No ID, no separate timestamp field** -- the timestamp lives inside the
  filename, not as its own field.
- **No pagination, no filtering, no "since" parameter.** One call returns
  every file ever recorded -- 964 entries in this capture. The page's own
  JS (`files_list_get()`) confirms this: it's a flat `GET`, no query
  params beyond the literal `filelist`.
- **Filename convention (parseable):** `YYMMDD_HHMM_<track/session
  name>_<driver name>.uni` (or `.un0` seen once -- unclear if that's a
  distinct/incomplete-recording extension, worth confirming next capture).
  Example: `260829_1441_Barmosen GPS_AUSTIN.uni`. The embedded date/time
  matches the file's internal `DATE` chunk exactly (see below), so the
  filename alone is enough to detect "is this new" without downloading
  anything, *if* filenames are guaranteed unique -- worth a second look
  since two files 34 minutes apart from the same track/driver differ only
  in the `HHMM` part, so a same-minute double-press could theoretically
  collide (untested).
- **`size` is reliable:** cross-checked directly -- the `size` field for
  the downloaded file (`913826`) matched the actual downloaded byte count
  exactly. Safe to use `size` (or filename) as a lightweight
  already-synced check without downloading first.
- **Example response (trimmed):**
  ```json
  {
    "files": [
      { "name": "240218_1753_003625.uni", "size": "25880" },
      { "name": "240223_1234_JOHNSON.uni", "size": "1994025" },
      { "name": "260829_1441_Barmosen GPS_AUSTIN.uni", "size": "913826" }
    ]
  }
  ```

**Polling caution:** the UI itself calls `/file?filelist` repeatedly while
open (roughly every 6-20s in this capture) -- so the device is clearly
built to tolerate that. Still worth keeping Part 2's polling conservative
and not treating "the UI does it" as license to poll faster than needed.

## Session-download endpoint

- **URL pattern:** `GET /file?filename=<name>` -- the page JS
  (`files_download(t)`) builds this by replacing spaces with `%20` only
  (`t.replace(" ", "%20")`) -- **not full URL-encoding**. Other special
  characters in a filename (there's at least one non-ASCII example in the
  file list, `Rødby`) are passed through as-is. Part 2's client should do
  proper URL-encoding of the filename regardless of what the device's own
  JS does, and should confirm the device accepts a properly-encoded
  request (untested here -- the only download exercised had a plain-ASCII
  name plus one space).
- **How to trigger it from the UI:** clicking a session's row / a download
  button creates a hidden `<a href="/file?filename=...">` and programmatically
  clicks it (standard browser-download trick) -- not relevant to Part 2
  beyond confirming the URL shape.
- **Response:** `200`, `Content-Type: application/octet-stream`, full
  file body, no chunking/streaming weirdness observed.
- **Repeatable/idempotent:** downloaded the same file 3 times in this
  capture -- all three responses were byte-for-byte identical (verified
  via checksum). Safe to retry a failed download.

## File format findings

- **Declared Content-Type on download:** `application/octet-stream`
  (uninformative, as anticipated).
- **Actual content:** **confirmed NOT TSV.** `analyze_har.py` never printed
  the TSV-match banner for this file -- it's a proprietary binary format.
  **Part 2 will need a real conversion step**, not a pass-through.
- **Format identified as a chunked/TLV binary container**, decoded as far
  as structure (not yet full sample-level decoding):
  - 4-byte magic: `"UUni"`, followed by 4 bytes `00 00 00 04` (meaning
    unconfirmed -- possibly a container/format version).
  - Then a sequence of **chunks**, each: 4-byte tag `"RECR"` + 4-byte
    ASCII chunk type + 1-byte flag (always `0x01` in this file) + 3-byte
    big-endian length + that many bytes of payload, then the next chunk
    immediately follows.
  - Chunks seen, in order, with sizes from the one file inspected:

    | Chunk type | Payload size | Contents (decoded) |
    |---|---|---|
    | `DATE` | 7 bytes | Session start timestamp -- decodes cleanly as `[unused, year-2000, month, day, hour, minute, second]`. For this file: `00 1a 08 1d 0e 29 14` -> 2026-08-29 14:41:20, which matches the filename (`260829_1441`) exactly. |
    | `DEVI` | 155 bytes | Embedded JSON device config, e.g. `{"unigo-one":{"serial_number":3625,"firmware_version":"1.20.002","gps_speed_delay":999,"Temp1":"PT1000","Temp2":"None","Flex":0,"Flex CH1":0,"Flex CH2":0}}` |
    | `INFO` | 4 bytes | Not decoded -- 4 raw bytes, meaning unknown. |
    | `GLOS` | 212 bytes | Starts with its own sub-magic `"UGse"`, then contains the session/track name as ASCII (`"Barmosen GPS"` for this file), null-padded. Likely a small metadata table; not fully mapped. |
    | `LOCS` | 428 bytes | Starts with sub-magic `"ULse"`. Contains the driver name (`"AUSTIN"`) and, oddly, **the filename of a different, earlier session** (`260829_1407_Barmosen GPS_AUSTIN.uni` -- 34 minutes before this one). Open question: is this a "previous session" backreference, a lap-count/continuity link across power cycles, or something else? Needs a second file to compare against (does every file point to the prior one, forming a chain?). |
    | `TRIG` | 2 bytes | Not decoded. |
    | `CHNL` | 112 bytes | **Channel definition table.** First 4 bytes = `27` (big-endian), matching a count; followed by 27 pairs of 2-byte values (`channel_id, type_or_width` -- e.g. `(1,3) (2,2) (3,2) (4,2) (5,2) (6,6) (7,6) (12,6) ... (77,2)`). 27 channels is very close to the 28 known Analyser TSV columns (`telemetry/parser.py::COLUMNS`) -- plausibly a near-1:1 mapping once the channel IDs are matched to column names, though not yet confirmed which ID means which column. |
    | `ELOG` | 20736 bytes | Not decoded -- "event log"? Large enough to be per-lap or per-event records rather than a single value. |
    | `SMRY` | 25604 bytes | Partially probed -- contains a recognizable pattern of repeated `7F FF FF FF 80 00 00 00` (int32 max immediately followed by int32 min), which reads like an uninitialized min/max-tracking sentinel pair -- consistent with a per-channel (or per-lap) min/max summary table where most slots are still at their sentinel "no data yet" value. Not decoded further. |
    | `DATA` | rest of file (866438 bytes here) | The actual telemetry sample stream. Its length field reads as the sentinel `0xFFFFFF` (all-1s in the 3-byte field) rather than a real length -- consistent with an embedded recorder that doesn't know the final size until it's done writing, and just means "read to end of file." **Internal layout not decoded** -- see below, this turned out to be the hard part. |

- **What's confirmed usable right now:** file identity (name/date/driver/
  track), device/firmware info, and the outer container structure. **What
  still needs work:** the actual per-sample byte layout inside `DATA`
  (sample rate, per-channel scaling/units, how it lines up with the 27
  channels from `CHNL`), and the `INFO`/`TRIG`/`ELOG`/`SMRY` chunks.

### DATA chunk reverse-engineering attempt (using a real TSV as ground truth)

A real Unipro Analyser TSV export of this exact session was obtained and
used as ground truth (the export actually contained 6 sessions
concatenated together -- filtered to the one matching `Start Date`/
`Start Time` = `2026-08-29`/`14:41:20`, which lined up with this file).
This confirms `telemetry/parser.py`'s existing understanding of the
format is accurate: every row is a sparse, asynchronous event -- most
columns blank, only the channel(s) that fired on that row populated.

Grouping the TSV's non-meta columns by which ones fire together on the
same row (i.e. which channels are reported as one atomic update) produces
exactly **9 distinct firing groups** for this session, e.g.:

| Group (TSV columns that co-fire) | Occurrences | Column count |
|---|---|---|
| `GPS Distance` alone | 35,610 | 1 |
| `RPM unfiltered`, `RPM` | 11,062 | 2 |
| `Steering Angle` alone | 11,056 | 1 |
| `Temperature 1`, `RPM unfiltered`, `RPM` | 10,324 | 3 |
| `Vertical DOP`, `Heading`, `Longitude`, `Altitude`, `Positional DOP`, `Horizontal DOP`, `Latitude` (GPS fix) | 7,382 | 7 |
| `GPS Speed` alone | 7,381 | 1 |
| `Vertical Acceleration`, `GPS Longitudinal Acceleration`, `GPS Lateral Acceleration` | 7,381 | 3 |
| `Internal Temperature`, `Temperature 1`, `RPM unfiltered`, `RPM`, `Battery Voltage` | 738 | 5 |
| (no data columns -- boundary/marker rows) | 22 | 0 |

The **group sizes line up suspiciously well with `CHNL`'s per-width
channel counts** (15 channels of width 6, 3 of width 3, 9 of width 2) --
e.g. the 7-column GPS fix group matches 7 consecutive width-6 channel IDs
(`6,7,12,13,14,15,16`) exactly. This is a real, encouraging structural
confirmation that the `CHNL` table does describe these groups -- but it
was not enough on its own to crack the byte layout: it was also noted
that other TSV columns (e.g. `Corner Radius`) don't appear in the
27-channel `CHNL` table at all, meaning some TSV columns are computed by
Analyser itself (e.g. `Corner Radius` = `1 / Inverse Corner Radius`) and
aren't a distinct raw channel, so the mapping isn't a clean 1:1 and needs
more care than "channel N = column N in file order."

**Framing hypotheses tried and ruled out:** assumed the event stream is a
flat sequence of `[channel_id][value bytes]` records (channel_id as
either 1 or 2 bytes, big- or little-endian, at every plausible starting
offset in the first 64 bytes of `DATA`) and greedily parsed forward,
checking whether it walks cleanly to the exact end of the 866,438-byte
payload. **None of these combinations got past 2 records** before hitting
a byte that isn't a valid channel ID -- so the raw stream is not a simple
flat `[id][value]` sequence. It's more likely bit-packed, uses
variable-length (varint-style) timestamps or deltas between records,
and/or groups multiple channels into one framed record (matching the
9 firing-groups above) rather than tagging every single value
individually. Cracking it fully would need more dedicated
reverse-engineering effort (or the firmware/format spec, which isn't
available) -- diminishing returns for a single sitting of blind byte
analysis.

### Recommended path for Part 2's conversion step

Given the above, two real options exist for Part 2, and this is a genuine
decision point rather than a purely technical one:

1. **Continue reverse-engineering `DATA`'s internal byte layout.** Now
   backed by real ground truth (this TSV) and a promising structural lead
   (the 9 firing-groups matching `CHNL`'s width groupings), this is
   plausibly crackable with more focused effort -- but it's open-ended,
   and any subtle mistake (wrong scale factor, wrong signedness) would
   silently produce wrong lap times/speeds rather than an obvious error.
2. **Automate the official Unipro Analyser desktop software** (the
   fallback "Plan B" from the original project plan) to do the `.uni` ->
   TSV conversion for us, since it's now proven to correctly export this
   exact device's files. This trades "no dependency on Analyser being
   installed" for "zero risk of a hand-decoded value being subtly wrong,"
   since Analyser's own conversion logic is used verbatim.

Given the format resisted a first serious reverse-engineering attempt and
correctness (not just "some path to TSV") matters for lap-time analysis,
**Plan B is the more defensible default for Part 2** unless further
reverse-engineering effort is explicitly wanted. This is a call for
whoever's driving Part 2, not something to decide silently -- flagged
here for that conversation.

## Open questions

- Full byte layout of the `DATA` chunk (sample rate, per-channel
  encoding/scaling, byte width per channel vs. the `CHNL` table). Tried
  and ruled out: flat `[channel_id][value]` framing (1 or 2 byte id,
  either endianness) -- see "DATA chunk reverse-engineering attempt"
  above. Next things worth trying: bit-packed fields, varint-encoded
  timestamps/deltas between records, or per-group framed records
  (matching the 9 firing-groups found in the TSV) instead of per-value
  tagging.
- Meaning of `INFO` (4 bytes), `TRIG` (2 bytes), `ELOG` (20736 bytes),
  `SMRY` (25604 bytes) chunks. `SMRY` shows a repeating int32-max/int32-min
  sentinel pair pattern consistent with an uninitialized min/max summary
  table -- worth decoding first since on-device lap summaries could be a
  shortcut, if confirmed.
- Which of the 27 `CHNL` channel IDs maps to which TSV column name --
  the 9 observed firing-groups line up with `CHNL`'s width groupings by
  count, but the ID-to-name mapping itself isn't confirmed (and at least
  one TSV column, `Corner Radius`, isn't a raw channel at all -- it's
  computed from `Inverse Corner Radius` by Analyser, so not every TSV
  column has a 1:1 channel).
- Whether `LOCS`'s embedded "previous session filename" is a chain link
  across all files (would need to check a second file to confirm).
- The `.un0` file extension seen once in the file list (vs. the usual
  `.uni`) -- different/incomplete recording state?
- Exact device AP SSID pattern (laptop was already joined before this
  capture started).
- Whether filenames can collide (same track/driver within the same
  minute) -- listing has no separate unique ID field, so this matters for
  a robust already-synced check.
- Whether the device tolerates more frequent polling than the ~6-20s
  interval its own UI uses, or whether that's close to a practical
  ceiling.
- `/file?delete=` and `/update` are known to exist (from the page's own
  JS) but were never exercised in this capture -- not needed for Part 2,
  but good to know they're there (and to avoid ever hitting `delete`
  accidentally).

## Raw capture references

- One real HAR capture, 2026-09-05, via mitmproxy against a live UniGo
  device at `192.168.4.1`: page load, `/status`, four `/file?filelist`
  calls, three downloads of `260829_1441_Barmosen GPS_AUSTIN.uni`
  (913826 bytes, byte-identical across all three). 11 HTTP requests
  total, 8 flagged interesting by `analyze_har.py`.
- A real Unipro Analyser TSV export (`.zip` containing one `.tsv`),
  2026-09-05, covering 6 sessions from the same device; the one matching
  `2026-08-29 14:41:20` was isolated and used as ground truth against the
  `.uni` file from the HAR capture above.
- The raw `.har` file, the `.uni` session file, and the TSV export all
  contain the user's real session filenames (track names, dates, and a
  driver name), so none of them are committed to the repo -- treat them
  as personal data, not project documentation. Only the findings derived
  from them (this file) are checked in.
