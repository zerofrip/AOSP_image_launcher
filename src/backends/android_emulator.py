"""Android Emulator backend for ranchu/goldfish only."""

from __future__ import annotations

import socket
from collections.abc import Callable

from backends.base import Backend
from capabilities import AccelSelection, Capabilities
from models import ArtifactMapping, CommandSpec, LaunchOptions, ProductArtifacts

_REQUIRED_FLAGS = ("-sysdir", "-kernel", "-ramdisk", "-system", "-memory")


class AndroidEmulatorBackend(Backend):
    name = "emulator"

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
        reasons: list[str] = []
        ignored: dict[str, str] = {}
        roles: dict[str, str] = {}

        if product.family != "emulator_ranchu":
            errors.append(
                f"Android Emulator backend supports ranchu/goldfish only; family is {product.family}"
            )
            if product.family == "cuttlefish":
                errors.append("Cuttlefish must be launched with launch_cvd, not the Android Emulator")
            return _unbootable(
                capabilities,
                errors,
                reasons=["emulator backend refused"],
                ignored=_ignore_special(product, options),
            )

        kernel = options.kernel or product.kernel
        ramdisk = options.ramdisk or product.ramdisk
        system = options.system or product.system
        vendor = options.vendor or product.vendor
        userdata = options.userdata or product.userdata

        if product.incomplete or kernel is None or ramdisk is None or system is None:
            errors.append(
                "emulator_ranchu requires kernel, ramdisk, and system.img (incomplete PRODUCT_OUT)"
            )
        if options.vendor_boot is not None:
            errors.append("vendor_boot must not be passed as -initrd or -drive")
        if product.vendor_boot is not None:
            ignored[str(product.vendor_boot)] = "never -initrd or -drive"
        if product.super_image is not None:
            ignored[str(product.super_image)] = "super.img is not a regular disk"
            warnings.append("super.img present; not attached as a regular disk")
        if product.dynamic_partitions:
            warnings.append("dynamic partitions noted; not mapped as ordinary emulator disks")
        if options.extra_args and not options.allow_extra_args:
            errors.append("--extra-arg requires --allow-extra-args")

        emulator = capabilities.emulator
        if not emulator.available or emulator.path is None:
            errors.append(emulator.error or "Android Emulator executable not found")
            return _unbootable(
                capabilities,
                errors,
                reasons=reasons,
                ignored=ignored,
                warnings=warnings,
            )

        help_blob = help_text or ""
        if not help_blob:
            errors.append("emulator -help output was not provided; refusing to guess flags")
            return _unbootable(
                capabilities,
                errors,
                reasons=reasons,
                ignored=ignored,
                warnings=warnings,
                executable=emulator.path,
            )

        missing_flags = [flag for flag in _REQUIRED_FLAGS if not _has_flag(help_blob, flag)]
        if missing_flags:
            errors.append("emulator -help is missing required flags: " + ", ".join(missing_flags))

        if errors:
            return _unbootable(
                capabilities,
                errors,
                reasons=reasons,
                ignored=ignored,
                warnings=warnings,
                executable=emulator.path,
            )

        argv: list[str] = [str(emulator.path), "-sysdir", str(product.product_out)]
        roles["sysdir"] = str(product.product_out)
        argv.extend(["-kernel", str(kernel)])
        roles["kernel"] = str(kernel)
        argv.extend(["-ramdisk", str(ramdisk)])
        roles["ramdisk"] = str(ramdisk)
        argv.extend(["-system", str(system)])
        roles["system"] = str(system)
        if vendor is not None and _has_flag(help_blob, "-vendor"):
            argv.extend(["-vendor", str(vendor)])
            roles["vendor"] = str(vendor)
        elif vendor is not None:
            warnings.append("emulator -help does not list -vendor; vendor.img not passed")
        if userdata is not None and _has_flag(help_blob, "-data"):
            argv.extend(["-data", str(userdata)])
            roles["data"] = str(userdata)
        elif userdata is not None:
            warnings.append("emulator -help does not list -data; userdata not passed")
        argv.extend(["-memory", str(int(options.memory_mb))])
        if _has_flag(help_blob, "-cores"):
            argv.extend(["-cores", str(int(options.cpus))])
        if _has_flag(help_blob, "-accel"):
            # The Android Emulator frontend accepts auto/off/on, not the
            # underlying QEMU accelerator names. TCG is exposed as "off".
            emulator_accel = "off" if accel.selected == "tcg" else "on"
            argv.extend(["-accel", emulator_accel])
        else:
            warnings.append("emulator -help does not list -accel")
        if options.headless:
            if _has_flag(help_blob, "-no-window"):
                argv.append("-no-window")
            else:
                warnings.append("headless requested but emulator -help does not list -no-window")
        if options.writable_system and _has_flag(help_blob, "-writable-system"):
            argv.append("-writable-system")
        if options.read_only and _has_flag(help_blob, "-read-only"):
            argv.append("-read-only")
        if options.snapshot and _has_flag(help_blob, "-snapshot"):
            argv.append("-snapshot")
        elif not options.snapshot and _has_flag(help_blob, "-no-snapshot"):
            argv.append("-no-snapshot")
        if options.append:
            if _has_flag(help_blob, "-qemu"):
                argv.extend(["-qemu", "-append", options.append])
            else:
                warnings.append("--append ignored; emulator -help does not list -qemu")

        console_port = int(options.adb_port) - 1
        adb_port = int(options.adb_port)
        if adb_port <= 1:
            errors.append(f"invalid --adb-port {options.adb_port}")
        elif _has_flag(help_blob, "-ports"):
            checker = port_in_use or default_port_in_use
            busy = [port for port in (console_port, adb_port) if checker(port)]
            if busy:
                errors.append(
                    "console/ADB ports in use: " + ", ".join(str(p) for p in busy)
                    + " (goldfish uses emulator -ports, not slirp hostfwd)"
                )
            else:
                argv.extend(["-ports", f"{console_port},{adb_port}"])
        else:
            warnings.append("emulator -help does not list -ports; ADB port mapping skipped")

        extra_applied = False
        if options.extra_args:
            if not options.allow_extra_args:
                errors.append("--extra-arg requires --allow-extra-args")
            else:
                argv.extend(options.extra_args)
                extra_applied = True

        if any("hostfwd" in item or "netdev" in item or "slirp" in item for item in argv):
            errors.append("goldfish ADB must not use slirp hostfwd")

        bootable = not errors
        if bootable:
            reasons.append("ranchu kernel+ramdisk+system present; emulator flags probed")
            if accel.fallback:
                warnings.append(accel.reason)
            reasons.append(accel.reason)
        else:
            reasons.extend(errors)

        return CommandSpec(
            argv=argv if bootable else [],
            backend=self.name,
            executable=emulator.path,
            mapping=ArtifactMapping(roles=roles, ignored=ignored),
            warnings=warnings,
            errors=errors,
            support_state="supported" if bootable else "unsupported",
            bootable=bootable,
            reasons=reasons,
            extra_args_applied=extra_applied,
        )


def default_port_in_use(port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"),):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
        except OSError:
            return True
        finally:
            sock.close()
    return False


def _has_flag(help_text: str, flag: str) -> bool:
    return flag in help_text


def _ignore_special(product: ProductArtifacts, options: LaunchOptions) -> dict[str, str]:
    ignored: dict[str, str] = {}
    if product.vendor_boot is not None:
        ignored[str(product.vendor_boot)] = "never -initrd or -drive"
    if options.vendor_boot is not None:
        ignored[str(options.vendor_boot)] = "never -initrd or -drive"
    if product.super_image is not None:
        ignored[str(product.super_image)] = "super.img is not a regular disk"
    return ignored


def _unbootable(
    capabilities: Capabilities,
    errors: list[str],
    *,
    reasons: list[str],
    ignored: dict[str, str],
    warnings: list[str] | None = None,
    executable=None,
) -> CommandSpec:
    return CommandSpec(
        argv=[],
        backend="emulator",
        executable=executable if executable is not None else capabilities.emulator.path,
        mapping=ArtifactMapping(ignored=ignored),
        warnings=list(warnings or []),
        errors=errors,
        support_state="unsupported",
        bootable=False,
        reasons=list(reasons) + list(errors),
        extra_args_applied=False,
    )
