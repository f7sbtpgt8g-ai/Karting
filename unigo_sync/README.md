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
  session library. A GUI app and a manual CLI both wrap the same portable
  core.
- **Part 3 (the GUI, login, and offline upload queue):** a proper
  end-user front end -- sign in, pick which driver a sync is for, choose
  how far back to look, click "Connect & Sync". Login is cached locally
  so it survives the laptop having no internet route at all while
  connected to the device's own WiFi (see "The GUI app" below), and
  anything downloaded while offline uploads itself automatically the
  moment the network is back, no user action needed.

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
python -m unigo_sync.cli sync --ingest --period last_week   # today (default) / last_week / last_month / all
python -m unigo_sync.cli watch --ingest   # polls every poll_interval_s until Ctrl+C
python -m unigo_sync.cli status           # shows sync history (synced/failed, per session)
```

`--driver`/`--track`/`--session-type` are passed straight through to
`SessionLibrary.save_session` and are optional -- omit them to ingest with
no metadata attached, same as a bare `scripts/ingest.py` run. `--period`
is filtered on the device's own filenames before anything is downloaded
(see `core/period.py`), so `--period all` is the only choice that can be
slow on a device with a long history.

### The GUI app

```bash
pip install -r unigo_sync/requirements.txt -r unigo_sync/requirements-windows.txt
python -m unigo_sync.platform_windows.gui_app
```

This is what `UniGoSyncSetup.exe` (below) actually installs and launches.
It's a small `tkinter` window (no extra GUI dependency) with two screens:

1. **Sign in** -- the same email/password check as the Streamlit app's own
   login (`telemetry.auth`, local or Supabase depending on
   `SUPABASE_URL`/`SUPABASE_ANON_KEY`/`SUPABASE_DB_URL` -- see
   "Pointing it at a real deployment" below). **Do this once while the
   laptop has normal internet access, before connecting to the UniGo
   device's WiFi.** A successful sign-in is cached to disk
   (`auth_cache_path` in `config.yaml`) for a week, the same lifetime as
   the server-side session it wraps, so the app still knows who's signed
   in even once the laptop joins the device's own access point and loses
   its route to the internet entirely.
2. **Settings & sync** -- pick:
   - **Driver**: your own account's driver profile by default, or any
     other driver profile on the platform (including one you add on the
     spot via "+ Add new driver..."), for the shared-laptop-at-the-track
     case where one machine is syncing several people's loggers in a row.
   - **Sync period**: `Today only` (the default -- fastest, since the
     device is only asked to list what it has, and the date embedded in
     each session's own filename is checked before anything is
     downloaded), `Last 7 days`, `Last 30 days`, or `Everything on the
     device`.
   - **Auto-sync whenever this laptop joins the UniGo device's WiFi**
     (optional, off by default) -- same watcher the old tray app had,
     checking the current SSID via `netsh wlan show interfaces` (see
     `platform_windows/wifi.py`), now wired to the same login/driver/
     period settings instead of a bare unattributed download.

   Clicking **Connect & Sync** downloads and decodes whatever's new and
   in the chosen period into the staging folder (`output_dir`) exactly as
   before, then tries to upload it into the sessions database right away.
   If the database isn't reachable -- the normal state while connected to
   the device's own offline AP -- each session is queued locally instead
   (`pending_uploads_db`) and a background check (every 15s) uploads the
   whole queue automatically the instant the database becomes reachable
   again, with no button to click and no re-sync needed.

Windows-only (the GUI app lives in `platform_windows/`, same as the wifi
watcher); the CLI above works identically on Linux/Mac for testing or
non-Windows use, since all the actual sync/decode/upload logic lives in
the portable core, not the GUI wrapper. The older bare tray icon
(`python -m unigo_sync.platform_windows.tray_app`) still exists for
anyone who wants the original account-free "just decode to a folder, no
login, no upload" behaviour, but is no longer what the installer ships.

#### Pointing it at a real deployment

By default the GUI checks credentials against, and uploads sessions into,
the local `sessions_db` SQLite file (`data/sessions.db`) -- fine for
trying it out, but a shared file, not a shared database, if more than one
laptop needs to see the same drivers. To point it at a real Supabase
project instead (the same one the Streamlit app itself would use), set
`supabase_url` / `supabase_anon_key` / `supabase_db_url` in `config.yaml`
-- see the comments there. These are read once, at startup, and mirrored
into the process's own `SUPABASE_URL`/`SUPABASE_ANON_KEY`/
`SUPABASE_DB_URL` environment variables (`core/config.py`'s
`load_config`), the same variables `telemetry.auth.provider_from_env` and
`telemetry.storage.session_library_from_env` already look for -- there's
just no convenient way to set a Windows environment variable on a
per-user, no-admin install, so `config.yaml` is the place to put them
instead.

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
3. Launch "UniGo Sync" from the Start Menu -- it's the same GUI app
   described above (sign in, then Connect & Sync), just with the Python
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
| `sync_state_db` | SQLite file tracking what's already synced (downloaded) |
| `sessions_db` | Local SQLite session library path, used when no Supabase database is configured (see below) |
| `auth_cache_path` | Cached login (email/session token/chosen driver), so a sign-in survives an offline sync pass |
| `pending_uploads_db` | SQLite queue of sessions downloaded but not yet uploaded (e.g. while offline) |
| `log_path` | Sync log file |
| `poll_interval_s` | How often `watch`/the GUI's auto-sync poll for new sessions on the device |
| `wifi_ssid_prefix` | SSID prefix the Windows layer looks for (`unigo-`) |
| `supabase_url` / `supabase_anon_key` / `supabase_db_url` | Optional -- point the GUI's login and uploads at a real Supabase project instead of `sessions_db`; see "Pointing it at a real deployment" above |

Unrecognized keys are kept under `SyncConfig.extra` rather than raising,
so a newer config file stays loadable by older code.

## Architecture

```
unigo_sync/
  cli.py                   <- OS-agnostic CLI: sync / watch / status
  ingest_bridge.py          <- bridges a sync result into telemetry's SessionLibrary
  config.yaml                <- real, confirmed endpoint/path values
  core/                        <- portable: plain requests/stdlib/pandas, no OS-specific code
    config.py                    <- SyncConfig dataclass + YAML loader (+ config.yaml -> env var bridge)
    device_client.py              <- HTTP calls to the device (list/download), retries, unsafe-URL guard
    uni_format.py                  <- .uni binary decoder (the reverse-engineered core)
    tsv_writer.py                    <- decoded DataFrame -> telemetry.parser-compatible TSV
    sync_state.py                     <- SQLite dedup: what's already been downloaded
    period.py                          <- filename-based date parsing + today/week/month/all cutoffs
    sync_engine.py                      <- orchestrates: list -> filter by period -> download -> decode -> write -> record
    auth_session.py                      <- login/driver-selection logic, wraps telemetry.auth/accounts
    auth_cache.py                         <- persists a signed-in session to disk for offline use
    connectivity.py                        <- is the sessions database reachable right now?
    pending_uploads.py                      <- SQLite queue: downloaded but not yet uploaded
    sync_orchestrator.py                     <- ties sync_engine + upload-or-queue + queue-flush together
  platform_windows/            <- Windows-only, thin: wraps core/ for a human on Windows
    wifi.py                        <- netsh-based current-SSID detection
    gui_app.py                      <- tkinter login + settings + "Connect & Sync" front end (primary)
    tray_app.py                      <- older, account-free bare tray icon (no login, no upload)
  packaging/                   <- turns gui_app.py into an installable Windows .exe
    run_gui.py                      <- tiny PyInstaller entry point (imports + calls gui_app.main) -- what installer.iss packages
    run_tray.py                      <- same, for the legacy tray_app.py, kept for anyone building that variant themselves
    UniGoSync.spec                    <- PyInstaller spec: onefile, windowed, icon
    make_icon.py                       <- generates icon.ico matching the tray icon's look
    installer.iss                       <- Inno Setup script: per-user install, shortcuts, uninstaller
  discovery/                   <- Part 1: capture-and-inspect harness (see discovery/README.md)
  tests/                        <- pytest suite for everything above except gui_app.py/tray_app.py's live GUI loops
```

The **portable-core / platform-layer split** is deliberate: `core/` never
imports anything Windows-specific, so a future iOS port (see `findings.md`
/ original design notes) only needs to replace `platform_windows/` with a
Swift equivalent -- the protocol and format knowledge in `core/` and
`config.yaml` carries over directly.

### Sync flow (`core/sync_engine.run_sync`)

1. `device_client.list_sessions()` -- GET the device's file-listing
   endpoint, parse `{name, size}` per session.
2. Any session outside the requested sync period (see `core/period.py`) is
   dropped here, before anything is downloaded -- the filename's own
   embedded date/time is checked, not the device's `size` field, so this
   costs nothing even on a device with hundreds of old sessions.
3. For each remaining session not already recorded as synced in
   `sync_state.py` (keyed by name + size, so a same-name-different-size
   file is treated as new rather than silently skipped): download the raw
   bytes, refusing to build a request URL for any filename that looks like
   it could hit the device's destructive `?delete=`/update endpoints
   (`_looks_unsafe`).
4. `uni_format.decode_uni_bytes()` turns the raw `.uni` bytes into a
   pandas DataFrame using the offsets/formulas confirmed in `findings.md`.
5. `tsv_writer.write_tsv()` writes it to `output_dir` as
   `<session-name>.unigo_sync.tsv`, in the exact column/quoting/line-ending
   shape `telemetry/parser.py` expects.
6. The attempt (success or failure, with error text) is recorded in
   `sync_state.py`'s SQLite DB so a re-run doesn't re-download or re-decode
   it.

A decode failure on one session (corrupt download, unexpected format) is
recorded as `failed` and does not stop the rest of that sync pass from
completing. This whole flow only ever needs the device's own local
address -- it runs identically whether or not the laptop has any other
network route.

### Upload flow (`core/sync_orchestrator`)

Uploading (getting a decoded TSV into the sessions database under the
chosen driver) is deliberately a separate step from the download/decode
above, gated on `core/connectivity.py`'s `is_online()`:

- **CLI, `--ingest`:** `ingest_bridge.py` calls `telemetry.parser.load_sessions`
  + `SessionLibrary.save_session` (or the Supabase-backed sibling, via
  `session_library_from_env` -- same choice `scripts/ingest.py` makes) on
  each newly-written TSV, synchronously, right after the sync pass.
- **GUI, "Connect & Sync":** `sync_orchestrator.sync_and_upload` does the
  same for each newly-synced session *if* the database is reachable right
  now. If it isn't -- the normal state while connected to the device's own
  offline AP -- the session is written into `pending_uploads.py`'s SQLite
  queue instead of being lost or retried in a loop. A background check in
  `gui_app.py` (every 15s) calls `sync_orchestrator.flush_pending_uploads`,
  which is a no-op with an empty queue or while still offline, and drains
  the whole queue automatically the moment the database answers again --
  the "connect to normal WiFi and it just uploads" behaviour needs no
  further sync pass or user action once that happens.

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
