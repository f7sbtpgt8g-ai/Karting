"""Entry point PyInstaller freezes into UniGoSync.exe -- calls
platform_windows.gui_app.main(), the login + settings + "Connect & Sync"
front end. Kept as a tiny standalone script (rather than pointing
PyInstaller at the package directly) because PyInstaller's Analysis needs
a concrete script file to start from -- same reasoning as the older
run_tray.py this replaces as the packaged entry point.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from unigo_sync.platform_windows.gui_app import main  # noqa: E402

if __name__ == "__main__":
    main()
