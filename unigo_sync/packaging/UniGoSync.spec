# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the packaged Windows build.

Produces a single UniGoSync.exe with the Python runtime and all
dependencies (tkinter, requests, pandas, psycopg2, ...) bundled in --
nothing else needs to be installed on the end user's machine. config.yaml
is deliberately NOT bundled as PyInstaller data: it's placed next to the
.exe by the installer (installer.iss) instead, so it stays a plain,
editable text file after install rather than being locked inside the
onefile archive. core/config.py's _default_config_path() looks next to
sys.executable specifically to match this.

Entry point is run_gui.py (platform_windows/gui_app.py's login + settings
+ "Connect & Sync" window) -- the older bare tray icon (run_tray.py /
platform_windows/tray_app.py, no login, no upload) is still importable
for anyone building a stripped-down variant themselves, but is no longer
what this installer ships.

Build with:
    pyinstaller unigo_sync/packaging/UniGoSync.spec
(run make_icon.py first to produce icon.ico alongside this file.)
"""

import os

REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))  # noqa: F821 (SPECPATH is spec-file builtin)
ICON_PATH = os.path.join(SPECPATH, "icon.ico")  # noqa: F821

a = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "run_gui.py")],  # noqa: F821
    pathex=[REPO_ROOT],
    binaries=[],
    datas=[],
    # Both are only ever actually used when a Postgres/Supabase deployment
    # sets SUPABASE_DB_URL/DATABASE_URL (telemetry/db.py, and pandas'
    # to_parquet(engine="pyarrow") in telemetry/storage.py's Supabase
    # session-cache path) -- neither import is visible to PyInstaller's
    # static analysis (one is behind a string-keyed pandas engine lookup,
    # the other only imported inside a function body), so both need to be
    # named explicitly or a Supabase-configured build silently breaks the
    # moment login/upload actually needs them.
    hiddenimports=["psycopg2", "pyarrow"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="UniGoSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_PATH,
)
