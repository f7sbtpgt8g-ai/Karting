"""Generates icon.ico for the packaged Windows build, reusing the same
plain-circle look drawn at runtime by platform_windows.tray_app so the
taskbar/installer icon matches the tray icon. Run once before pyinstaller
during packaging (see the build workflow) -- not needed for source/CLI use.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

_SIZE = 256
_COLOR = (30, 144, 255)


def make_icon(path: str) -> None:
    img = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = _SIZE // 10
    draw.ellipse([margin, margin, _SIZE - margin, _SIZE - margin], fill=_COLOR)
    img.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (128, 128), (256, 256)])


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "icon.ico")
    make_icon(out)
    print(f"wrote {out}")
