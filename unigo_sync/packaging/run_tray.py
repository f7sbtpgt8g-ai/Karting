"""Entry point PyInstaller freezes into UniGoSync.exe -- just calls
platform_windows.tray_app.main(). Kept as a tiny standalone script
(rather than pointing PyInstaller at the package directly) because
PyInstaller's Analysis needs a concrete script file to start from.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from unigo_sync.platform_windows.tray_app import main  # noqa: E402

if __name__ == "__main__":
    main()
