# unigo_sync

A tool to pull session data off a Unipro UniGo laptimer automatically (over
the device's own WiFi access point), decode it, and feed it into this
repo's analysis pipeline -- so returning from a track day means the data
is already there, no manual export/copy step.

## Status: Part 1 + Part 2 both done

- **Part 1 (discovery):** real device captures decoded the `.uni` binary
  format channel-by-channel against real Analyser TSV ground truth across
  5 sessions on 3 tracks. Full writeup, including what didn't work (G-force
  channels) and why, lives in [`findings.md`](findings.md).
- **Part 2 (this tool):** a working sync tool built directly on those
  findings -- lists sessions on the device, downloads new ones, decodes
  `.uni` into the TSV shape `telemetry/parser.py` expects, tracks what's
  already been synced, and optionally ingests straight into the analysis
  session library. A Windows system-tray front end and manual CLI both
  wrap the same portable core.

## Quick start

```bash
pip install -r unigo_sync/requirements.txt
```

Connect to the device's WiFi (`unigo-xxxx`), then:

```bash
python -m unigo_sync.cli sync --ingest
```

This downloads any session the device has that hasn't been synced before,
writes a `.unigo_sync.tsv` file for each into `data/unigo_sync/incoming/`,
and (with `--ingest`) loads the new ones straight into
`data/sessions.db` via the same loading code `scripts/ingest.py` uses --
from there they show up in the Streamlit app like any other ingested
session.

Other commands:

```bash
python -m unigo_sync.cli sync --ingest --driver "Alice" --track "Barmosen" --session-type practice
python -m unigo_sync.cli watch --ingest   # polls every poll_interval_s until Ctrl+C
python -m unigo_sync.cli status           # shows sync history (synced/failed, per session)
```

`--driver`/`--track`/`--session-type` are passed straight through to
`SessionLibrary.save_session` and are optional -- omit them to ingest with
no metadata attached, same as a bare `scripts/ingest.py` run.

### Windows tray app

```bash
pip install -r unigo_sync/requirements.txt -r unigo_sync/requirements-windows.txt
python -m unigo_sync.platform_windows.tray_app
```

Gives a tray icon with "Sync now" and a toggleable "Auto-sync when
connected to UniGo WiFi" background watcher (checks the current SSID via
`netsh wlan show interfaces` -- see `platform_windows/wifi.py`). This is
Windows-only; the CLI above works identically on Linux/Mac for testing or
non-Windows use, since all the actual sync/decode logic lives in the
portable core, not the tray wrapper.

### Installing on an end user's Windows PC (no Python needed)

For someone who just wants to plug in and go -- no Python, no pip, no
terminal -- download and run **`UniGoSyncSetup.exe`** instead:

1. Go to the repo's
   [Actions tab](../../actions/workflows/build-windows-installer.yml),
   open the latest successful run of "Build UniGo Sync Windows installer",
   and download the `UniGoSyncSetup` artifact (a zip containing the one
   `.exe`).
2. Run `UniGoSyncSetup.exe`. It installs per-user (no admin rights or UAC
   prompt needed), adds a Start Menu entry, and offers optional checkboxes
   for a desktop shortcut and starting automatically with Windows.
3. Launch "UniGo Sync" from the Start Menu -- it's the same tray app
   described above (Sync now / auto-sync toggle), just with the Python
   runtime and all dependencies bundled in.

`config.yaml` is installed as a plain, editable text file next to
`UniGoSync.exe` (under `%LocalAppData%\Programs\UniGoSync\`) -- edit it
there directly if an endpoint ever needs to change; it won't be
overwritten by a reinstall/upgrade.

This has been built and verified end-to-end on a real Windows GitHub
Actions runner (see the workflow linked above), not just written and
assumed to work.

### Building the installer yourself

```bash
pip install -r unigo_sync/requirements.txt -r unigo_sync/requirements-windows.txt
python unigo_sync/packaging/make_icon.py unigo_sync/packaging/icon.ico
pyinstaller --distpath dist --workpath build unigo_sync/packaging/UniGoSync.spec
# then, with Inno Setup (https://jrsoftware.org/isinfo.php) installed:
iscc unigo_sync/packaging/installer.iss
```

Produces `unigo_sync/packaging/Output/UniGoSyncSetup.exe`. All of this
must run on Windows (PyInstaller freezes for the OS it runs on, and
`ISCC.exe` is Windows-only) -- see
[`.github/workflows/build-windows-installer.yml`](../.github/workflows/build-windows-installer.yml)
for the exact, CI-verified sequence, including installing Inno Setup via
Chocolatey. That workflow runs automatically on any push touching
`unigo_sync/**`, or on demand via the Actions tab's "Run workflow" button.

## Configuration

All endpoints, timeouts, and local paths live in
[`config.yaml`](config.yaml), loaded via `core/config.py`'s `SyncConfig`.
Defaults match a real device on firmware 1.20.002 (see `findings.md`'s
"Device info" / endpoint table). If a firmware update changes an endpoint,
re-run the discovery harness (`discovery/`) against the updated device and
edit the relevant value here -- this is meant to be a config edit, not a
code change:

| Key | Meaning |
| --- | --- |
| `base_url` | Device AP address, e.g. `http://192.168.4.1` |
| `filelist_path` | Session-listing endpoint (JSON: name + size per file) |
| `download_path_template` | Download URL, `{name}` is URL-encoded |
| `request_timeout_s` / `download_timeout_s` | Per-request HTTP timeouts |
| `max_retries` / `retry_backoff_s` | Retry policy for a flaky device connection |
| `output_dir` | Where decoded `.tsv` files are written |
| `sync_state_db` | SQLite file tracking what's already synced |
| `log_path` | Sync log file |
| `poll_interval_s` | How often `watch`/the tray auto-sync poll |
| `wifi_ssid_prefix` | SSID prefix the Windows layer looks for (`unigo-`) |

Unrecognized keys are kept under `SyncConfig.extra` rather than raising,
so a newer config file stays loadable by older code.

## Architecture

```
unigo_sync/
  cli.py                   <- OS-agnostic CLI: sync / watch / status
  ingest_bridge.py          <- bridges a sync result into telemetry's SessionLibrary
  config.yaml                <- real, confirmed endpoint/path values
  core/                        <- portable: plain requests/stdlib/pandas, no OS-specific code
    config.py                    <- SyncConfig dataclass + YAML loader
    device_client.py              <- HTTP calls to the device (list/download), retries, unsafe-URL guard
    uni_format.py                  <- .uni binary decoder (the reverse-engineered core)
    tsv_writer.py                    <- decoded DataFrame -> telemetry.parser-compatible TSV
    sync_state.py                     <- SQLite dedup: what's already been downloaded
    sync_engine.py                     <- orchestrates: list -> download -> decode -> write -> record
  platform_windows/            <- Windows-only, thin: wraps core/ for a human on Windows
    wifi.py                        <- netsh-based current-SSID detection
    tray_app.py                      <- pystray tray icon front end
  packaging/                   <- turns tray_app.py into an installable Windows .exe
    run_tray.py                     <- tiny PyInstaller entry point (imports + calls tray_app.main)
    UniGoSync.spec                    <- PyInstaller spec: onefile, windowed, icon
    make_icon.py                       <- generates icon.ico matching the tray icon's look
    installer.iss                       <- Inno Setup script: per-user install, shortcuts, uninstaller
  discovery/                   <- Part 1: capture-and-inspect harness (see discovery/README.md)
  tests/                        <- pytest suite for everything above except tray_app.py's live pystray loop
```

The **portable-core / platform-layer split** is deliberate: `core/` never
imports anything Windows-specific, so a future iOS port (see `findings.md`
/ original design notes) only needs to replace `platform_windows/` with a
Swift equivalent -- the protocol and format knowledge in `core/` and
`config.yaml` carries over directly.

### Sync flow (`core/sync_engine.run_sync`)

1. `device_client.list_sessions()` -- GET the device's file-listing
   endpoint, parse `{name, size}` per session.
2. For each session not already recorded as synced in `sync_state.py`
   (keyed by name + size, so a same-name-different-size file is treated as
   new rather than silently skipped): download the raw bytes, refusing to
   build a request URL for any filename that looks like it could hit the
   device's destructive `?delete=`/update endpoints (`_looks_unsafe`).
3. `uni_format.decode_uni_bytes()` turns the raw `.uni` bytes into a
   pandas DataFrame using the offsets/formulas confirmed in `findings.md`.
4. `tsv_writer.write_tsv()` writes it to `output_dir` as
   `<session-name>.unigo_sync.tsv`, in the exact column/quoting/line-ending
   shape `telemetry/parser.py` expects.
5. The attempt (success or failure, with error text) is recorded in
   `sync_state.py`'s SQLite DB so a re-run doesn't re-download or re-decode
   it.
6. If `--ingest` was passed, `ingest_bridge.py` calls
   `telemetry.parser.load_sessions` + `SessionLibrary.save_session` on
   each newly-written TSV -- the same functions `scripts/ingest.py` uses,
   just triggered automatically instead of via a manual file-path
   argument.

A decode failure on one session (corrupt download, unexpected format) is
recorded as `failed` and does not stop the rest of that sync pass from
completing.

## Known limitations (carried over from findings.md)

- **Time base is modeled, not measured.** No literal high-precision
  timestamp field was found in the format; elapsed time is reconstructed
  from an assumed-uniform 10Hz GPS-fix cadence (index x 100ms), linearly
  interpolated by byte offset for other record types. This matched real
  Analyser exports closely in every session tested, but is a documented
  modeling choice, not a decoded field.
- **Steering Angle** decodes with R^2 = 0.82-0.88 against ground truth --
  real but imperfect, unlike RPM/GPS/battery/temperature which are exact.
- **Vertical Acceleration, GPS Longitudinal/Lateral Acceleration, Slip,
  Corner Radius, GPS Total Acceleration, Steering Rate, and "Time"** are
  never populated -- left blank in the output TSV. These were confirmed,
  after exhaustive attempts across multiple real sessions, to either not
  be recoverable from the raw format or (GPS Lateral Acceleration) to be
  indistinguishable from Steering Angle's own physical correlation with
  cornering force, not a separate decoded channel. See findings.md's
  "G-force channels" section for the full negative-result writeup.
- **Housekeeping record's Battery Voltage / Internal Temperature byte
  ranges overlap by one byte** and were never independently re-derived to
  a clean boundary -- both decode to plausible values but this is a known,
  unresolved ambiguity rather than a fully pinned-down offset. Same for
  the GPS-fix record's PDOP/HDOP/VDOP fields.
- **Lap-number detection** (beacon-crossing, ported from OpenLap's
  technique) has a known artifact on some sessions' final lap (observed as
  a merged-lap boundary in the one real multi-track validation run) --
  lap *boundaries* mid-session were consistently accurate, only the tail
  end showed this.
- **`config.yaml`'s default lookup path depends on `sys.frozen`**
  (`core/config.py`'s `_default_config_path`): in a normal source checkout
  it resolves relative to the package, but in the packaged `.exe` it looks
  next to `sys.executable` instead, since PyInstaller's onefile builds
  extract to an ephemeral temp directory that `__file__` would otherwise
  point at. This is why the installer places `config.yaml` beside
  `UniGoSync.exe` as a plain file rather than bundling it inside the
  archive.

## If a firmware update breaks sync

Re-run the discovery harness (`discovery/README.md`) against the updated
device, diff the new report against `findings.md`, and update
`config.yaml` and, if the `.uni` byte layout itself changed, the offsets
in `core/uni_format.py`.
