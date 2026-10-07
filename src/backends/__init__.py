"""Launch backends (Android Emulator and fail-closed QEMU)."""

from __future__ import annotations

from backends.android_emulator import AndroidEmulatorBackend
from backends.qemu import QemuBackend

__all__ = ["AndroidEmulatorBackend", "QemuBackend"]
