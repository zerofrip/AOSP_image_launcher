"""Launcher error types. Exit code 2 is used for validation failures."""

from __future__ import annotations


class LauncherError(Exception):
    exit_code = 2

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ProductOutError(LauncherError):
    """PRODUCT_OUT could not be resolved or is unusable."""


class AmbiguousProductOutError(ProductOutError):
    def __init__(self, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates


class ArchitectureError(LauncherError):
    """Guest architecture is missing, not x86_64, or conflicting."""


class AcceleratorError(LauncherError):
    """Requested accelerator is unavailable or invalid for this host/executable."""


class BackendError(LauncherError):
    """Selected backend cannot launch the detected product."""


class AospBuildError(LauncherError):
    """AOSP source-tree validation, lunch, or build failed."""


class UserdataError(LauncherError):
    """Instance userdata copy/reset was refused or failed."""


class PortError(LauncherError):
    """ADB/console port is invalid or in use."""
