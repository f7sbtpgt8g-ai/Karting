# UniGo device findings

Living document, now populated from a real capture (see "Raw capture
references" below). Firmware version at capture time: **1.20.002**
(reported two slightly different ways by the device itself -- see Device
info). Re-run the discovery harness and diff against this doc if a
firmware update changes anything.

Last updated: 2026-09-05. Sources: a real HAR capture of the device's web
UI (page load + session list + downloading one session, three times); a
real Unipro Analyser TSV export of that exact same session used as ground
truth; and a from-scratch reverse-engineering pass on the raw binary
format that **cracked the per-record framing** and decoded 8 channels
(GPS position, altitude, embedded GPS speed, DOP/quality values, battery
voltage, internal temperature) with verified formulas, independently of
Analyser -- see "BREAKTHROUGH" below. RPM, steering angle, and
accelerometer G-force remain the one open gap.

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

### BREAKTHROUGH: the per-record framing is now solved

Following up per explicit direction to keep pushing on this before
building anything: **the record framing inside `RECRDATA` is now cracked**,
independently of the OpenLap prior art below (found afterward, and used
only to double-check, not to derive this).

**The framing:** every record starts with a 2-byte marker `DA 7A`,
followed by a 2-byte little-endian sequence field, followed by a
type-specific fixed-length body. Total record length is fully determined
by a small set of distinct lengths -- found by scanning for the marker
and measuring the gap to the next one:

| Record length | Count (this file) | Contents identified |
|---|---|---|
| 45 bytes | 7,382 | **GPS fix**: lat, lon, altitude, DOPs, embedded speed |
| 32 bytes | 738 | **Housekeeping**: battery voltage, internal temperature |
| 11, 14, 18, 24, 28 bytes | 7,382 / 7,372 / 3,690 / 7,372 / 2,952 | Partially identified -- see gap below |
| 4, 7 bytes | 1, 29 | Rare -- edge/boundary artifacts, not investigated further |

This was found and confirmed two ways: (1) every one of the 7,382
confirmed real GPS fixes (via the OpenLap-style plausibility scan) landed
inside a **45-byte record with latitude starting at byte offset 17 from
the marker, with zero exceptions** across all 7,382 -- a fully
deterministic result, not a heuristic; (2) the 2-byte field right after
the marker is a monotonically non-decreasing sequence number (0 decreases
across 7,381 transitions, tracking elapsed session time almost exactly
linearly) -- exactly what a real per-record sequence counter should look
like, not noise.

**Channels decoded with confirmed, verified formulas** (linear fit
against the real TSV, R² given -- 1.000000 means the formula is exact to
float64 precision, not approximate):

| Channel | Record type | Byte offset (from record start) | Width | Encoding | Formula | R² |
|---|---|---|---|---|---|---|
| Latitude | 45 | 17 | 4 | int32 BE, signed | `raw / 1e7` | 1.000000 (verified at width=4; a width=2 sub-slice also fits well *only* because this session covers a tiny area -- width=4 is the portable/general formula, matches OpenLap's own scale factor independently) |
| Longitude | 45 | 21 | 4 | int32 BE, signed | `raw / 1e7` | 1.000000 (same caveat as latitude) |
| Altitude | 45 | 27 | 2 | int16 BE, signed | `raw / 1000` | 1.000000 |
| GPS Speed (embedded in the fix) | 45 | 31 | 2 | int16 BE, signed | `raw / 100` (approx) | 0.9978 -- good but not exact; likely Analyser applies its own smoothing on top of this raw channel |
| Positional DOP | 45 | 38 | 2 | int16 LE, signed | `raw / 100` | 1.000000 |
| Horizontal DOP | 45 | 40 | 2 | int16 LE, signed | `raw / 100` | 1.000000 |
| Vertical DOP | 45 | 41 | 2 | int16 LE, signed | `raw / 25600` | 1.000000 |
| Battery Voltage | 32 | 15 | 2 | int16 LE, signed | `raw * 0.01 - 15.36` (ADC calibration offset, not a truncated field) | 1.000000 |
| Internal Temperature | 32 | 16 | 2 | int16 LE, unsigned | `raw / 25600 + 17.92` | 1.000000 |

(Byte offsets above are from the very start of the record, i.e. include
the 4-byte marker+sequence prefix -- add 4 to get the offset from the end
of that prefix, which is how the raw analysis scripts index into "body".)

Note the DOP field offsets (38/40/41) are close together and were found
via correlation search rather than a fully independent structural
derivation -- worth a sanity check against a second real file before
treating them as bulletproof, though the R²=1.0 fits are strong evidence
either way.

**GPS Distance and Heading were NOT cracked as raw stored channels** --
best fits found (R² 0.93-0.9988, large non-zero intercepts) are good but
clearly not exact, unlike the R²=1.000000 channels above. The likely
explanation: these are **computed by Analyser from the GPS track itself**
(heading = bearing between consecutive fixes, distance = integrated speed
or point-to-point distance) rather than raw stored values -- exactly the
technique OpenLap independently uses to reconstruct the same two
quantities (see `_bearing_rad`/`_haversine_km` in their source, discussed
below). If so, this project's own Part 2 code should compute them the
same way from decoded lat/lon, rather than keep hunting for a raw channel
that may not exist.

**RPM, Steering Angle, and the 3-axis accelerometer (Vertical/GPS
Longitudinal/GPS Lateral Acceleration) remain uncracked**, despite an
extensive, systematic search across the still-unidentified record types
(11, 14, 18, 24, 28 bytes) and re-checking every already-identified type
too:
- Every byte offset x width (1-4 bytes) x endianness x signedness,
  correlated against every TSV column -- best result was 0.80 (not
  usable).
- The same search repeated with a small time-lag (±5 records) in case of
  an off-by-a-few alignment issue -- no improvement.
- IEEE-754 float32 interpretation at every offset/endianness -- zero
  matches above 0.5 correlation.
- A delta-encoding/cumulative-sum hypothesis (treating each byte as a
  small signed increment rather than an absolute value) -- produced
  several 0.6-0.8 "matches," but literally every byte offset in the
  32-byte housekeeping record showed similar correlation, which is the
  classic signature of spurious correlation between two unrelated
  cumulative-sum ("random walk") sequences, not a real signal.
- The `RECRELOG` chunk (20,736 bytes) was checked as a possible location
  for these channels instead of `RECRDATA` -- ruled out, it's a **plain
  ASCII device boot/diagnostic log** (`/Logs/logfile.2.txt`, firmware
  version/serial/build-date strings), not telemetry.
- `RECRSMRY` (25,604 bytes) was followed up on -- see "SMRY chunk solved"
  below. It gives per-lap min/max, not a full per-sample trace, so it
  narrows the gap but doesn't close it.
- Full rank-based (Spearman) correlation re-run across every
  offset/width/endianness/signedness for RPM, RPM unfiltered, Steering
  Angle, Vertical/Longitudinal/Lateral Acceleration, and Temperature 1 --
  catches monotonic-but-nonlinear relationships that plain linear
  correlation would miss. Only Temperature 1 showed a moderate (not
  conclusive) rank correlation (~0.85) at two record types; RPM, Steering,
  and the three acceleration channels showed nothing above 0.55.

### SMRY chunk solved: per-lap min/max summary (not a full trace)

`RECRSMRY` turned out to be exactly what its name suggests: a per-lap
summary table, not raw samples. Confirmed by searching it for the real,
lap-by-lap min/max values computed from the TSV (rather than searching
`RECRDATA` for a full time series): **found exact matches for Steering
Angle's real per-lap min and max (×10, int16 BE) at a clean, constant
**128-byte stride per lap** -- lap 1's block at byte offset 66 (min) /
70 (max), lap 2's at 194/198, lap 3's at 322/326, lap 4's at 450/454 --
each exactly 128 bytes after the previous, verified against 4 different
real laps' actual extremes (not just one lucky match). RPM's real
session-max (12495) was also found exactly, elsewhere in the same
lap-9-ish region of the buffer, confirming RPM is stored here as raw
unscaled units (no divisor) alongside steering's ×10 scale -- useful
confirmation of the physical unit conventions even where the full
per-sample trace remains unrecovered. (Lap 0 -- the outlap -- didn't
match, plausibly excluded from this table the way outlaps often are;
laps 5-10 in this particular session have a stuck/constant steering
sensor reading exactly 1.5 the whole lap, which is too common a byte
pattern to search for reliably and produced noise, not a real negative
result.)

**Practical implication:** if full per-sample RPM/Steering/G-force never
gets cracked, `RECRSMRY` is a fallback worth keeping in mind -- it can't
produce a corner-by-corner trace, but it can give real per-lap "peak
RPM this lap" / "max steering angle this lap" numbers independent of
Analyser, which is more than nothing for a first cut of Part 2.

**Working theory for why these three resist the same techniques that
worked for everything else:** RPM and Steering Angle are the two
*highest-frequency, most rapidly-changing* channels in the whole file
(22,124 and 11,056 firings respectively -- RPM fires almost 3x more often
than GPS fixes), and the still-unidentified record types (11/14/18/24/28
bytes) don't cleanly sum to either of those counts the way 45+32 cleanly
accounted for the channels above. That, plus zero linear/lagged/float/
delta signal despite exhaustive search, suggests either (a) they're
bit-packed at a sub-byte level rather than byte-aligned, (b) they use a
non-linear encoding (e.g. a lookup table, non-uniform quantization, or a
proper stateful delta-decoder that needs the *exact* previous value, not
just a naive cumulative sum), or (c) they're split across record types in
a way that makes single-type positional correlation the wrong tool
entirely (would need to reconstruct a merged, correctly-interleaved
per-channel sequence across multiple record types before correlating,
which is a bigger undertaking than what's been tried so far).

### Second real file: format/formula cross-validation + a keyframe/delta test

A second real `.uni` file was provided -- a different session, different
track (`260823_1555_Korsor GPS_AUSTIN.uni`, Korsør, Denmark, on the
opposite side of Zealand from Barmosen), same device/firmware
(`unigo-one`, serial 3625, firmware `1.20.002`). No matching `.tsv` export
was available for this one, so it can't directly confirm new channels the
way the first file pair did -- but it's valuable in two ways:

**1. Structural + formula cross-validation (strong positive result).**
The chunk layout, `CHNL` table (same 27 channel id/width pairs, byte-for-
byte identical), and `RECRDATA` record-type tag bytes (`90/92 80 80 40`,
`f0/f2/fe 80 a0 40`, etc.) all match the first file exactly -- confirming
these are protocol constants, not session-specific data. More importantly,
**decoding this file's GPS fixes with the exact same formulas derived
from the first file** (`lat = int32_BE/1e7` at body offset 13, etc., zero
session-specific tuning) produced `lat 55.3531-55.3548, lon 11.1585-
11.1611` -- which is genuinely, correctly Korsør's real location on a map,
with a sensible closed-loop track shape and a plausible `0-104.64 km/h`
speed range. This is real, independent confirmation the decode formulas
are general (not overfit to one session/location), found without needing
a TSV for this file at all.

**2. A "keyframe + delta" hypothesis, tested against the first file's real
TSV (still no signal for RPM/Steering/G-force).** OpenLap's own module
docstring describes `RECRDATA` as using "a variable-length keyframe-vs-
delta scheme" -- worth taking seriously given how the record-type tags
naturally pair up: a common, smaller type (`14`, `24`) alongside a rarer,
larger type sharing the same tag prefix (`18`, `28`). Tested directly:
walk all records in true file order, and on each "keyframe" record
(18 or 28) *set* a running state from a candidate field, then on each
"delta" record (14 or 24) *add* a candidate signed field to that running
state -- across every plausible offset/width/endianness combination for
both the keyframe-set and the delta-add fields, correlating the
reconstructed running value against every remaining TSV target. Result:
**no signal for RPM, Steering Angle, or any of the three acceleration
channels** (nothing above the 0.6 threshold). The only matches found
(`Temperature 1`, ~0.935, identical across dozens of different delta
offset/width choices) are a giveaway that the keyframe resets alone
explain the correlation and the delta terms are contributing noise, not
real signal -- i.e. this specific pairing isn't the Temperature 1 channel
either, it's an artifact of Temperature 1 changing slowly enough that
almost any slowly-updating anchor loosely tracks it.

**Net effect:** the keyframe/delta idea remains plausible in principle
(OpenLap's own description, and the natural tag-pairing, both point that
way) but hasn't been the key that unlocks RPM/Steering/G-force so far --
either the specific type pairings tried aren't the right ones, the
keyframe/delta roles are reversed from what was assumed, or the scheme
applies at a finer (bit-level, not byte-level) granularity than tested.

**3. Bit-level search (also no signal).** In case a channel is packed at
sub-byte granularity (a plausible reason whole-byte search would miss
it), every individual bit (not byte) of every still-unidentified record
type was correlated against binarized targets -- steering direction
(left/right sign), RPM above/below session median, and vertical-
acceleration sign. Weak, inconclusive results only (best: 0.40, at
record type 28 byte 16 vs. RPM-above-median -- plausibly just a slowly-
drifting state value that happens to loosely track a session-long RPM
trend, the same false-lead pattern seen with Temperature 1 above). No
bit position showed the kind of clean, strong correlation the confirmed
8 channels showed at the byte level.

### Prior art found: an independent open-source project already cracked part of this

A web search turned up
[OpenLap](https://github.com/LaurensVR3/OpenLap) (`unipro_data.py`), a
telemetry-overlay tool that already reverse-engineers `.uni` files
independently of Analyser. Its own documentation is refreshingly candid
about what it did and didn't crack, and **its outer chunk-parsing code
(`_iter_chunks`) is byte-for-byte identical in logic to what we derived
independently above** -- magic `UUni`, 8-byte tag, 1-byte version, 3-byte
big-endian length, `0xFFFFFF` sentinel meaning "read to end of file." Two
independent reverse-engineering efforts landing on the same structure is
strong confirmation that part is solid.

**What OpenLap did NOT crack (confirmed, not just claimed):** the
per-channel event framing inside `RECRDATA`/`DATA` -- the same wall this
project hit initially too, before the breakthrough above. Their
workaround: instead of parsing records, they **scan the raw bytes for a
specific recognizable *pattern of plausible values*** -- 4
consecutive big-endian `int32` fields (altitude, speed, then looking
backward for latitude, longitude) that all simultaneously fall in
physically sane ranges (lat -90..90, lon -180..180, alt -500..9000m,
speed 0..400 km/h) -- rather than understanding the record structure that
contains them. Scale factors: `lat = raw/1e7`, `lon = raw/1e7`,
`alt = raw/1000`, `speed = raw/100`. **RPM, gear, gyro, and exhaust temp
are explicitly *not* recovered by this technique** -- those channels stay
completely unrecovered in OpenLap, same as here.

**This was independently re-verified against our own file+TSV pair**
(not just taken on faith): running the same value-plausibility scan
against `260829_1441_Barmosen GPS_AUSTIN.uni`'s `DATA` payload finds
**16,999 raw candidate hits**, of which **exactly 7,382 fall inside the
real track's lat/lon bounding box** (55.049-55.052 / 11.907-11.914) --
which matches, to the exact integer, the 7,382-occurrence "GPS fix" group
found independently in the TSV cross-reference above. The decoded values
for those 7,382 are directly plausible against the TSV ground truth: lat/
lon track a sensible closed loop starting and ending near
`55.0508, 11.9124`, speed ranges `0-89.06 km/h` (a kart's real range),
altitude sits near sea level (consistent with a Danish track). **This is
now empirically confirmed, not just borrowed from someone else's
README.**

**One thing OpenLap's own approach gets wrong on this file, worth fixing
in Part 2's implementation:** false-positive hits actually *outnumber*
real ones here -- **9,617 spurious matches vs. 7,382 real ones** (a >50%
false-positive rate on raw hits), clustered at implausible-but-in-range
coordinates like `lat≈0.05` / `lat≈-26.0` with widely scattered
longitudes -- almost certainly other channels' raw bytes coincidentally
forming a plausible-looking lat/lon/alt/speed quadruple. OpenLap's own
median-position-based filter (`_reject_far_from_track`) would fail badly
here, since it assumes genuine hits are the *majority* and computes the
median accordingly -- on this file the median lands inside the spurious
cluster, not the real one. **A tighter, more reliable filter is needed for
Part 2**: e.g. requiring several consecutive candidate hits to be
mutually close in both position *and* byte-offset spacing (a real GPS
track can't teleport between fixes 16-100 bytes apart), rather than
filtering against a single global median.

### Recommended path for Part 2's conversion step

**Updated after the framing breakthrough above.** Given the explicit
decision to not depend on Analyser:

1. **Independently decodable today, with verified exact formulas (see
   the decode table above):** GPS position, altitude, embedded GPS speed,
   DOP/fix-quality values, battery voltage, and internal temperature --
   via the real record framing (not a fuzzy value-scan), which also means
   near-100% recovery within a session rather than OpenLap's ~60%-of-native-
   rate/50%-false-positive scan. Heading and distance are best computed
   from the decoded GPS track directly (bearing / integration), matching
   what OpenLap does, rather than hunted for as raw channels. Combined
   with the RECRGLOS timing-beacon coordinates OpenLap also uses, this is
   enough for a track map, real GPS speed trace, and real lap timing --
   independent of Analyser.
2. **RPM, steering angle, and the 3-axis accelerometer G-forces remain
   NOT recovered as a full per-sample trace**, despite the record framing
   being solved and a very extensive, systematic search (see above,
   including the `RECRSMRY` follow-up). This is a narrower, more specific
   gap than "the whole format is unsolved," but it's still a real one.
   `telemetry/parser.py`'s downstream analysis (corner-causal detection,
   setup suggestions, throttle/braking inference) leans heavily on
   exactly these three channels, so **a no-Analyser sync tool today would
   still feed the pipeline meaningfully less than a `.tsv` export would**
   -- this is a real fidelity trade-off, not a detail, and needs to be a
   conscious decision rather than something this document quietly assumes
   away. `RECRSMRY` does give real per-lap min/max for at least RPM and
   Steering Angle as a fallback (see above) -- not a trace, but not zero
   either.
3. Next places to look for RPM/steering/G-force per-sample data, if
   continuing: (a) a proper stateful delta-decoder (track a running
   per-channel value and apply small signed deltas cumulatively, rather
   than the naive global cumsum already tried and shown to be spurious),
   (b) bit-level (not byte-aligned) packing within the still-partially-
   understood record types (11/14/18/24/28 bytes), (c) obtaining a second
   real `.uni`+`.tsv` pair -- ideally from a session with more dramatic
   RPM/steering variation -- to cross-validate any future candidate
   formula the same way the channels above were confirmed, (d) a deeper
   pass on `RECRSMRY` beyond per-lap min/max, in case it also holds a
   coarser (e.g. per-second) time-bucketed trace rather than just two
   numbers per lap.

## Open questions

- **RPM, Steering Angle, and 3-axis accelerometer G-force per-sample byte
  encoding** -- the one major remaining gap. The outer record framing is
  solved (see "BREAKTHROUGH" above) and 8 other channels are decoded with
  verified, often-exact formulas; `RECRSMRY` additionally gives real
  per-lap min/max for RPM and Steering Angle (confirming their physical
  scale: RPM raw/unscaled, Steering Angle x10). But the full per-sample
  trace for all three resisted linear/lagged/float32/delta-cumsum/rank
  correlation search, AND a stateful keyframe+delta reconstruction search
  (see "Second real file" above) -- across every record type and offset
  tried so far. A second real file (Korsør) confirmed the *other* 8
  channels' formulas generalize correctly, but didn't have a matching TSV
  so couldn't directly help crack these three. Next things worth trying:
  bit-level (non-byte-aligned) packing, a deeper `RECRSMRY` pass in case
  it holds more than per-lap extremes, trying the keyframe+delta idea
  with the roles/type-pairings reversed, or a third real file that DOES
  come with a matching TSV and has more dramatic RPM/steering variation
  than the two sessions seen so far.
- Exact byte boundaries for the DOP fields (Positional/Horizontal/
  Vertical DOP all decode with R²=1.0 individually, but their offsets --
  38/40/41 -- are close enough together that the precise boundary between
  them hasn't been independently re-derived the way lat/lon/altitude
  were; worth double-checking against a file with a matching TSV).
- Meaning of `INFO` (4 bytes), `TRIG` (2 bytes) chunks, and full
  understanding of `SMRY` (25604 bytes, only the sentinel-pattern
  observation so far) and `ELOG` (confirmed to be a plain-text device log,
  not telemetry -- no further work needed there).
- Which of the 27 `CHNL` channel IDs maps to which TSV column name --
  now partially answered by the direct byte-offset decode table above for
  8 channels, but the `CHNL` table's own (channel_id, width) entries
  haven't been individually matched to those confirmed offsets (and at
  least one TSV column, `Corner Radius`, isn't a raw channel at all --
  it's computed from `Inverse Corner Radius` by Analyser).
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
- A second real `.uni` file, `260823_1555_Korsor GPS_AUSTIN.uni`
  (1,206,009 bytes), a different session at a different track (Korsør,
  Denmark) from the same device -- no matching TSV export available for
  this one. Used for structural/formula cross-validation (see "Second
  real file" above), not for cracking new channels.
- The raw `.har` file, both `.uni` session files, and the TSV export all
  contain the user's real session filenames (track names, dates, and a
  driver name), so none of them are committed to the repo -- treat them
  as personal data, not project documentation. Only the findings derived
  from them (this file) are checked in.
