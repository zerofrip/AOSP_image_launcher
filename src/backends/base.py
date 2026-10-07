"""Launch backend protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from capabilities import AccelSelection, Capabilities
from models import CommandSpec, LaunchOptions, ProductArtifacts


class Backend(ABC):
    name: str

    @abstractmethod
    def plan(
        self,
        product: ProductArtifacts,
        options: LaunchOptions,
        capabilities: Capabilities,
        *,
        accel: AccelSelection,
        help_text: str | None = None,
        port_in_use: Callable[[int], bool] | None = None,
    ) -> CommandSpec:
        """Return a command spec. Never uses shell=True."""
