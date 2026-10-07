"""QEMU backend for v1.

QEMU is not used to boot Cuttlefish or ranchu/goldfish. For the generic
``unknown`` x86_64 family, qemu-system-x86_64 is used when a kernel, initrd,
and system.img are available and no unsupported images (vendor_boot, super.img,
dynamic partitions) are present.

vendor_boot is never -initrd/-drive; super.img is never an ordinary disk.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from backends.base import Backend
from capabilities import AccelSelection, Capabilities
from image_inspector import inspect_file
from models import ArtifactMapping, CommandSpec, LaunchOptions, ProductArtifacts

inspect_file  # referenced for future image validation; keep import stable

_ADB_DEFAULT_PORT = 5555


def _qemu_format(path: Path) -> str:
    """Return 'qcow2' for .qcow2 files, otherwise 'raw'."""
    return "qcow2" if str(path).endswith(".qcow2") else "raw"


def _fail(
    backend: "QemuBackend",
    capabilities: Capabilities,
    errors: list[str],
    warnings: list[str],
    ignored: dict[str, str],
    reasons: list[str],
) -> CommandSpec:
    return CommandSpec(
        argv=[],
        backend=backend.name,
        executable=capabilities.qemu.path,
        mapping=ArtifactMapping(roles={}, ignored=ignored),
        warnings=warnings,
        errors=errors,
        support_state="unsupported",
        bootable=False,
        reasons=reasons + errors,
        extra_args_applied=False,
    )


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
        warnings: list[str] = []
        reasons: list[str] = [
            "QEMU backend is fail-closed in v1 without a complete non-goldfish command evidence pack"
        ]
        ignored: dict[str, str] = {}

        # ── unchanged fail-closed blocks ────────────────────────────────────
        if product.family == "cuttlefish":
            errors.append(
                "Cuttlefish (vsoc_x86_64) is not supported by qemu-system-x86_64; use launch_cvd"
            )
        elif product.family == "emulator_ranchu":
            errors.append(
                "ranchu/goldfish requires Android Emulator goldfish pipe/sync/battery devices; "
                "plain qemu-system-x86_64 is unsupported"
            )

        # ── super.img / dynamic_partitions refusal (all families) ───────────
        if product.dynamic_partitions or product.super_image is not None:
            errors.append("super.img / dynamic partitions cannot be attached as an ordinary QEMU disk")
            if product.super_image is not None:
                ignored[str(product.super_image)] = "not an ordinary disk"

        # ── vendor_boot refusal (all families) ──────────────────────────────
        if product.vendor_boot is not None or options.vendor_boot is not None:
            errors.append("vendor_boot must not be passed as -initrd or -drive")
            path = options.vendor_boot or product.vendor_boot
            if path is not None:
                ignored[str(path)] = "never -initrd or -drive"

        # ── init_boot is always ignored ──────────────────────────────────────
        if product.init_boot is not None:
            ignored[str(product.init_boot)] = "init_boot is not used as QEMU initrd in v1"

        # ── extra_args gate (all families) ──────────────────────────────────
        if options.extra_args and not options.allow_extra_args:
            errors.append("--extra-arg requires --allow-extra-args")

        # ── early exit for non-unknown families or when errors already set ──
        if product.family in ("cuttlefish", "emulator_ranchu"):
            # legacy "missing kernel/ramdisk" messages kept exactly
            kernel = options.kernel or product.kernel
            ramdisk = options.ramdisk or product.ramdisk
            if kernel is None:
                errors.append("missing kernel evidence")
            if ramdisk is None:
                errors.append("missing initrd/ramdisk evidence")
            errors.append("missing complete kernel/initrd/cmdline evidence for a goldfish-free QEMU boot")
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

        if product.family != "unknown":
            errors.append(f"product family {product.family!r} has no QEMU evidence pack")
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # architecture guard
        if product.architecture != "x86_64":
            errors.append(
                f"qemu-system-x86_64 requires x86_64; got {product.architecture!r}"
            )
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # capabilities guard
        if not capabilities.qemu.available:
            errors.append("qemu-system-x86_64 executable not found")
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # kernel + ramdisk resolution
        kernel = options.kernel or product.kernel
        ramdisk = options.ramdisk or product.ramdisk
        _tmpdir: str | None = None
        if kernel is None or ramdisk is None:
            boot_img = product.boot
            if boot_img is not None and capabilities.unpack_bootimg.available:
                _tmpdir = tempfile.mkdtemp()
                try:
                    subprocess.run(
                        [
                            str(capabilities.unpack_bootimg.path),
                            "--boot_img",
                            str(boot_img),
                            "--out",
                            _tmpdir,
                        ],
                        check=True,
                        capture_output=True,
                    )
                except (subprocess.CalledProcessError, OSError) as exc:
                    errors.append(f"unpack_bootimg failed: {exc}")
                else:
                    k = Path(_tmpdir) / "kernel"
                    r = Path(_tmpdir) / "ramdisk"
                    if k.exists():
                        kernel = kernel or k
                    if r.exists():
                        ramdisk = ramdisk or r
            if kernel is None:
                errors.append(
                    "missing kernel: provide --kernel or a PRODUCT_OUT with kernel or boot.img + unpack_bootimg"
                )
            if ramdisk is None:
                errors.append(
                    "missing ramdisk/initrd: provide --ramdisk or a PRODUCT_OUT with ramdisk or boot.img + unpack_bootimg"
                )
            if errors:
                return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # system image required
        system = options.system or product.system
        if system is None:
            errors.append("missing system image: provide --system or a PRODUCT_OUT with system.img")
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # if we already have cross-family errors (vendor_boot/super.img/extra_args), bail
        if errors:
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # build argv
        exe = str(capabilities.qemu.path)
        argv = [exe]

        # accel
        argv += ["-accel", accel.selected]
        if accel.fallback:
            warnings.append(f"accelerator fallback to {accel.selected}: {accel.reason}")

        # machine / memory / cpus
        argv += ["-machine", "pc"]
        argv += ["-m", str(options.memory_mb)]
        argv += ["-smp", str(options.cpus)]

        # kernel + initrd + cmdline
        argv += ["-kernel", str(kernel)]
        argv += ["-initrd", str(ramdisk)]
        cmdline = "console=ttyS0 androidboot.hardware=qemu"
        if options.append:
            cmdline += " " + options.append
        argv += ["-append", cmdline]

        # system drive
        sys_fmt = _qemu_format(system)
        sys_drive = f"file={system},format={sys_fmt},if=virtio"
        if not options.writable_system:
            sys_drive += ",readonly=on"
        argv += ["-drive", sys_drive]
        roles = {"system": str(system)}

        # optional drives: vendor, product, system_ext, userdata
        for role, src in [
            ("vendor", options.vendor or product.vendor),
            ("product", options.product_image or product.product),
            ("system_ext", options.system_ext or product.system_ext),
            ("data", options.userdata or product.userdata),
        ]:
            if src is not None:
                fmt = _qemu_format(src)
                drv = f"file={src},format={fmt},if=virtio,readonly=on"
                argv += ["-drive", drv]
                roles[role] = str(src)

        # networking (user-mode slirp + hostfwd for ADB)
        adb_port = int(options.adb_port)
        argv += [
            "-netdev", f"user,id=net0,hostfwd=tcp::{adb_port}-:5555",
            "-device", "virtio-net-pci,netdev=net0",
        ]

        # serial + safety
        argv += ["-serial", "mon:stdio", "-no-reboot"]

        # headless / snapshot
        if options.headless:
            argv += ["-nographic"]
        if options.snapshot:
            argv += ["-snapshot"]

        # extra args
        extra_args_applied = False
        if options.extra_args and options.allow_extra_args:
            argv += list(options.extra_args)
            extra_args_applied = True

        reasons.append(
            f"generic x86_64 QEMU boot: kernel={kernel}, ramdisk={ramdisk}, system={system}"
        )

        return CommandSpec(
            argv=argv,
            backend=self.name,
            executable=capabilities.qemu.path,
            mapping=ArtifactMapping(roles=roles, ignored=ignored),
            warnings=warnings,
            errors=[],
            support_state="supported",
            bootable=True,
            reasons=reasons,
            extra_args_applied=extra_args_applied,
        )
