"""Assemble launch plans and format argv without shell=True."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from backends.android_emulator import AndroidEmulatorBackend
from backends.qemu import QemuBackend
from capabilities import (
    AccelProbe,
    AccelSelection,
    Capabilities,
    RunnerFn,
    probe_accelerators,
    probe_emulator_help,
    select_accelerator,
)
from errors import AcceleratorError, BackendError
from models import ArtifactMapping, CommandSpec, LaunchOptions, LaunchPlan, ProductArtifacts

_SECRET_FLAGS = frozenset(
    {
        "--password",
        "-password",
        "--token",
        "-token",
        "--secret",
        "-secret",
        "--pass",
        "-pass",
        "--auth",
        "-auth",
    }
)
_SECRET_KEYS = ("password", "token", "secret", "passwd", "auth")


def format_command(argv: Sequence[str], *, windows: bool = False) -> str:
    """Format argv for display. Never for execution (execution uses the list)."""

    items = [str(part) for part in argv]
    if windows:
        return subprocess.list2cmdline(items)
    return shlex.join(items)


def redact_argv(argv: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for raw in argv:
        item = str(raw)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        if item in _SECRET_FLAGS:
            redacted.append(item)
            hide_next = True
            continue
        lowered = item.lower()
        if "=" in item and any(f"{key}=" in lowered for key in _SECRET_KEYS):
            key, _, _rest = item.partition("=")
            redacted.append(f"{key}=<redacted>")
            continue
        redacted.append(item)
    return redacted


def format_command_redacted(argv: Sequence[str], *, windows: bool = False) -> str:
    return format_command(redact_argv(argv), windows=windows)


def build_launch_plan(
    product: ProductArtifacts,
    options: LaunchOptions,
    capabilities: Capabilities,
    *,
    accel_probe: AccelProbe | None = None,
    accel: AccelSelection | None = None,
    emulator_help: str | None = None,
    runner: RunnerFn | None = None,
    port_in_use: Callable[[int], bool] | None = None,
) -> LaunchPlan:
    """Select backend + accel and build a CommandSpec. Does not spawn the guest."""

    backend_name = (options.backend or "auto").strip().lower()
    if backend_name not in {"auto", "emulator", "qemu"}:
        raise BackendError(f"unknown backend {options.backend!r}")

    help_text = emulator_help
    if help_text is None and capabilities.emulator.path is not None and backend_name in {"auto", "emulator"}:
        probed = probe_emulator_help(capabilities.emulator.path, runner=runner)
        help_text = "\n".join(part for part in (probed.stdout, probed.stderr) if part)
        if probed.error and not help_text:
            help_text = ""

    selection = accel
    probe = accel_probe
    try:
        if selection is None:
            executable, kind = _accel_executable(backend_name, product, capabilities)
            if probe is None and executable is not None:
                probe = probe_accelerators(executable, kind, runner=runner)
            if probe is None:
                probe = AccelProbe(
                    executable=executable,
                    kind=kind,
                    reported=(),
                    raw_output="",
                    error="accel probe skipped (no executable)",
                )
            selection = select_accelerator(
                options.accel,
                capabilities.host,
                probe,
                executable=executable,
            )
    except AcceleratorError as exc:
        spec = CommandSpec(
            argv=[],
            backend=backend_name if backend_name != "auto" else _preferred_backend_name(product),
            executable=_backend_executable(backend_name, product, capabilities),
            mapping=ArtifactMapping(),
            errors=[str(exc)],
            support_state="unsupported",
            bootable=False,
            reasons=[str(exc)],
        )
        return LaunchPlan(
            product=product,
            spec=spec,
            accelerator=options.accel,
            memory_mb=options.memory_mb,
            cpus=options.cpus,
            adb_port=options.adb_port,
            headless=options.headless,
            read_only=options.read_only,
        )

    spec = _plan_for_backend(
        backend_name,
        product,
        options,
        capabilities,
        accel=selection,
        help_text=help_text,
        port_in_use=port_in_use,
    )
    return LaunchPlan(
        product=product,
        spec=spec,
        accelerator=selection.selected,
        memory_mb=options.memory_mb,
        cpus=options.cpus,
        adb_port=options.adb_port,
        headless=options.headless,
        read_only=options.read_only,
    )


def _plan_for_backend(
    backend_name: str,
    product: ProductArtifacts,
    options: LaunchOptions,
    capabilities: Capabilities,
    *,
    accel: AccelSelection,
    help_text: str | None,
    port_in_use: Callable[[int], bool] | None,
) -> CommandSpec:
    emulator = AndroidEmulatorBackend()
    qemu = QemuBackend()
    if backend_name == "emulator":
        return emulator.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
    if backend_name == "qemu":
        return qemu.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
    # auto
    if product.family == "emulator_ranchu":
        spec = emulator.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
        if spec.bootable:
            return spec
        qemu_spec = qemu.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
        spec.errors = list(spec.errors) + [
            "auto: emulator plan is not bootable; QEMU is fail-closed for ranchu"
        ]
        spec.reasons = list(spec.reasons) + list(qemu_spec.reasons)
        spec.bootable = False
        spec.argv = []
        spec.support_state = "unsupported"
        return spec
    if product.family == "cuttlefish":
        emu_spec = emulator.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
        qemu_spec = qemu.plan(
            product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
        )
        errors = [
            "Cuttlefish is unsupported by the Android Emulator and qemu-system-x86_64 backends",
            "use launch_cvd from the Cuttlefish host package",
        ]
        return CommandSpec(
            argv=[],
            backend="cuttlefish",
            executable=None,
            mapping=ArtifactMapping(
                ignored={
                    **emu_spec.mapping.ignored,
                    **qemu_spec.mapping.ignored,
                }
            ),
            warnings=emu_spec.warnings + qemu_spec.warnings,
            errors=errors + emu_spec.errors + qemu_spec.errors,
            support_state="unsupported",
            bootable=False,
            reasons=errors,
        )
    # For unknown/generic families, delegate to QEMU and return its result as-is.
    # The qemu backend is authoritative — if it can boot, bootable=True; if not, fail-closed.
    qemu_spec = qemu.plan(
        product, options, capabilities, accel=accel, help_text=help_text, port_in_use=port_in_use
    )
    return qemu_spec


def _preferred_backend_name(product: ProductArtifacts) -> str:
    if product.family == "emulator_ranchu":
        return "emulator"
    if product.family == "cuttlefish":
        return "cuttlefish"
    return "qemu"


def _accel_executable(
    backend_name: str, product: ProductArtifacts, capabilities: Capabilities
) -> tuple[Path | None, str]:
    if backend_name == "qemu":
        return capabilities.qemu.path, "qemu"
    if backend_name == "emulator":
        return capabilities.emulator.path, "emulator"
    if product.family == "emulator_ranchu" and capabilities.emulator.path is not None:
        return capabilities.emulator.path, "emulator"
    if capabilities.qemu.path is not None:
        return capabilities.qemu.path, "qemu"
    if capabilities.emulator.path is not None:
        return capabilities.emulator.path, "emulator"
    return None, "qemu"


def _backend_executable(
    backend_name: str, product: ProductArtifacts, capabilities: Capabilities
) -> Path | None:
    path, _kind = _accel_executable(backend_name, product, capabilities)
    return path
