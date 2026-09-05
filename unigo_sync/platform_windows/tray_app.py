"""System tray app: the friendly, double-clickable front end for
unigo_sync on Windows. Wraps `core.sync_engine.run_sync` -- all it adds
is a tray icon, a manual "Sync Now" action, and an optional background
watcher that triggers a sync automatically once it detects the machine
has joined the device's WiFi (`platform_windows.wifi`).

Requires `pystray` and `Pillow`, which are Windows-friendly but not
listed in the portable core's dependencies -- see ../requirements-windows.txt.
Not runnable/testable outside Windows; keep all device/format logic this
depends on in ../core so that logic itself stays fully tested without
needing a Windows machine.
"""

from __future__ import annotations

import logging
import threading
import time

from ..core.config import SyncConfig, load_config
from ..core.sync_engine import configure_logging, run_sync
from .wifi import is_connected_to_unigo

logger = logging.getLogger("unigo_sync.platform_windows.tray_app")

_ICON_SIZE = 64


def _make_icon_image(color: tuple[int, int, int]):
    """A plain solid-colour circle, generated in code so the app doesn't
    need a bundled .ico asset for this default look. Swap in a real
    icon file here (and reference it in the PyInstaller spec) for a
    packaged build if a nicer one is wanted."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse([margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin], fill=color)
    return img


class TrayApp:
    def __init__(self, config: SyncConfig | None = None):
        self.config = config or load_config()
        configure_logging(self.config)
        self._watching = False
        self._watch_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._icon = None  # set in run(), after pystray is imported

    # -- actions --------------------------------------------------------

    def sync_now(self, icon=None, item=None) -> None:
        threading.Thread(target=self._sync_now_worker, daemon=True).start()

    def _sync_now_worker(self) -> None:
        logger.info("manual sync triggered")
        try:
            result = run_sync(self.config)
        except Exception:
            logger.exception("sync failed")
            self._notify("Sync failed -- check the log for details")
            return
        if result.new_synced:
            self._notify(f"Synced {len(result.new_synced)} new session(s)")
        elif result.failed:
            self._notify(f"{len(result.failed)} session(s) failed to sync -- check the log")
        else:
            logger.info("nothing new to sync")

    def toggle_watch(self, icon=None, item=None) -> None:
        self._watching = not self._watching
        if self._watching:
            self._stop_event.clear()
            self._watch_thread = threading.Thread(target=self._watch_worker, daemon=True)
            self._watch_thread.start()
            logger.info("background watcher enabled")
        else:
            self._stop_event.set()
            logger.info("background watcher disabled")

    def _is_watching(self, item) -> bool:
        return self._watching

    def _watch_worker(self) -> None:
        while not self._stop_event.is_set():
            if is_connected_to_unigo(self.config.wifi_ssid_prefix):
                self._sync_now_worker()
            self._stop_event.wait(self.config.poll_interval_s)

    def _notify(self, message: str) -> None:
        logger.info(message)
        if self._icon is not None:
            try:
                self._icon.notify(message, title="UniGo Sync")
            except Exception:
                pass  # notifications aren't critical -- the log always has it

    def quit(self, icon=None, item=None) -> None:
        self._stop_event.set()
        if self._icon is not None:
            self._icon.stop()

    # -- entry point ------------------------------------------------------

    def run(self) -> None:
        import pystray
        from pystray import MenuItem as Item

        menu = pystray.Menu(
            Item("Sync now", self.sync_now, default=True),
            Item("Auto-sync when connected to UniGo WiFi", self.toggle_watch, checked=self._is_watching),
            pystray.Menu.SEPARATOR,
            Item("Quit", self.quit),
        )
        self._icon = pystray.Icon("unigo_sync", _make_icon_image((30, 144, 255)), "UniGo Sync", menu)
        self._icon.run()


def main() -> None:
    TrayApp().run()


if __name__ == "__main__":
    main()
