"""Fail-closed QEMU backend for v1.

QEMU is not used to boot Cuttlefish or ranchu/goldfish. argv stays empty and
bootable is False unless a future evidence pack is complete. vendor_boot is
never -initrd/-drive; super.img is never an ordinary disk.
"""

from __future__ import annotations

from collections.abc import Callable

from backends.base import Backend
from capabilities import AccelSelection, Capabilities
from models import ArtifactMapping, CommandSpec, LaunchOptions, ProductArtifacts


class QemuBackend(Backend):
    name = "qemu"

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
        errors: list[str] = []
        reasons: list[str] = [
            "QEMU backend is fail-closed in v1 without a complete non-goldfish command evidence pack"
        ]
        ignored: dict[str, str] = {}
        if product.family == "cuttlefish":
            errors.append(
                "Cuttlefish (vsoc_x86_64) is not supported by qemu-system-x86_64; use launch_cvd"
            )
        elif product.family == "emulator_ranchu":
            errors.append(
                "ranchu/goldfish requires Android Emulator goldfish pipe/sync/battery devices; "
                "plain qemu-system-x86_64 is unsupported"
            )
        else:
            errors.append(f"product family {product.family!r} has no QEMU evidence pack")
        if product.dynamic_partitions or product.super_image is not None:
            errors.append(
                "super.img / dynamic partitions cannot be attached as an ordinary QEMU disk"
            )
            if product.super_image is not None:
                ignored[str(product.super_image)] = "not an ordinary disk"
        if product.vendor_boot is not None or options.vendor_boot is not None:
            errors.append("vendor_boot must not be passed as -initrd or -drive")
            path = options.vendor_boot or product.vendor_boot
            if path is not None:
                ignored[str(path)] = "never -initrd or -drive"
        if product.init_boot is not None:
            ignored[str(product.init_boot)] = "init_boot is not used as QEMU initrd in v1"
        kernel = options.kernel or product.kernel
        ramdisk = options.ramdisk or product.ramdisk
        if kernel is None:
            errors.append("missing kernel evidence")
        if ramdisk is None:
            errors.append("missing initrd/ramdisk evidence")
        errors.append("missing complete kernel/initrd/cmdline evidence for a goldfish-free QEMU boot")
        if options.extra_args and not options.allow_extra_args:
            errors.append("--extra-arg requires --allow-extra-args")
        return CommandSpec(
            argv=[],
            backend=self.name,
            executable=capabilities.qemu.path,
            mapping=ArtifactMapping(roles={}, ignored=ignored),
            warnings=[],
            errors=errors,
            support_state="unsupported",
            bootable=False,
            reasons=reasons + errors,
            extra_args_applied=False,
        )
