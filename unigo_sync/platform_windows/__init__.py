"""Windows-only platform layer for unigo_sync -- WiFi SSID detection and
the system tray app. Everything that actually talks to the device or
decodes its data lives in ../core (portable, OS-agnostic); this package
is deliberately thin and Windows-specific, so a future iOS port only has
to replace this layer, not the device/format logic. See ../README.md.
"""
