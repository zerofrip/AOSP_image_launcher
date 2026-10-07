"""Detect host, tools, and accelerators. Probe failures are diagnostics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from errors import AcceleratorError
from paths import detect_wsl

PROBE_TIMEOUT_SEC = 5.0
_WALK_LIMIT = 48
_ACCEL_NAMES = ("kvm", "whpx", "tcg", "hax", "hvf", "nvmm", "xen")
WhichFn = Callable[[str], str | None]
RunnerFn = Callable[[Sequence[str]], "ProbeResult"]


@dataclass(frozen=True)
class ProbeResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


@dataclass(frozen=True)
class HostEnvironment:
    platform: str  # windows | linux | wsl
    is_windows: bool
    is_linux: bool
    is_wsl: bool
    kvm_node_exists: bool
    kvm_accessible: bool
    cpu_count: int
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: Path | None
    available: bool
    details: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class AccelProbe:
    executable: Path | None
    kind: str
    reported: tuple[str, ...]
    raw_output: str
    error: str | None = None


@dataclass(frozen=True)
class AccelSelection:
    requested: str
    selected: str
    fallback: bool
    reason: str
    available: tuple[str, ...]


@dataclass
class Capabilities:
    host: HostEnvironment
    emulator: ToolInfo
    qemu: ToolInfo
    emulator_check: ToolInfo
    unpack_bootimg: ToolInfo
    lpunpack: ToolInfo
    simg2img: ToolInfo
    avbtool: ToolInfo
    diagnostics: list[str] = field(default_factory=list)

    def tool(self, name: str) -> ToolInfo:
        return getattr(self, name)


def detect_host(
    *,
    environ: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    is_wsl: bool | None = None,
    kvm_path: str | Path | None = "/dev/kvm",
    cpu_count: int | None = None,
    osrelease_text: str | None = None,
) -> HostEnvironment:
    env = dict(environ) if environ is not None else dict(os.environ)
    plat = (platform_name or sys.platform).lower()
    diagnostics: list[str] = []
    native_windows = plat.startswith("win")
    wsl = bool(is_wsl) if is_wsl is not None else (False if native_windows else detect_wsl(env, osrelease_text))
    if native_windows:
        platform = "windows"
    elif wsl:
        platform = "wsl"
    else:
        platform = "linux"
    kvm_exists = False
    kvm_ok = False
    if kvm_path is not None and platform != "windows":
        node = Path(kvm_path)
        try:
            kvm_exists = node.exists()
        except OSError as exc:
            diagnostics.append(f"cannot stat {node}: {exc}")
        if kvm_exists:
            try:
                kvm_ok = os.access(node, os.R_OK | os.W_OK)
            except OSError as exc:
                diagnostics.append(f"cannot access {node}: {exc}")
            if not kvm_ok:
                diagnostics.append(f"{node} exists but is not accessible")
        else:
            diagnostics.append(f"{node} does not exist")
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return HostEnvironment(
        platform=platform,
        is_windows=platform == "windows",
        is_linux=platform in {"linux", "wsl"},
        is_wsl=platform == "wsl",
        kvm_node_exists=kvm_exists,
        kvm_accessible=kvm_ok,
        cpu_count=max(1, int(cpus)),
        diagnostics=tuple(diagnostics),
    )


def executable_format(path: Path | None) -> str:
    """Return 'pe', 'elf', or 'unknown' from magic bytes. Never uses hostname."""

    if path is None:
        return "unknown"
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        name = path.name.lower()
        if name.endswith(".exe"):
            return "pe"
        return "unknown"
    if magic.startswith(b"MZ"):
        return "pe"
    if magic.startswith(b"\x7fELF"):
        return "elf"
    if path.name.lower().endswith(".exe"):
        return "pe"
    return "unknown"


def discover_tools(
    *,
    environ: Mapping[str, str] | None = None,
    product_out: Path | None = None,
    host: HostEnvironment | None = None,
    which: WhichFn | None = None,
    is_file: Callable[[Path], bool] | None = None,
) -> Capabilities:
    env = dict(environ) if environ is not None else dict(os.environ)
    host_info = host if host is not None else detect_host(environ=env)
    which_fn = which or (lambda name: shutil.which(name, path=env.get("PATH")))
    is_file_fn = is_file or _is_file
    diagnostics: list[str] = list(host_info.diagnostics)

    emulator = find_emulator(
        environ=env,
        product_out=product_out,
        host=host_info,
        which=which_fn,
        is_file=is_file_fn,
    )
    qemu = _tool_from_which(
        "qemu-system-x86_64",
        which_fn,
        extra_names=("qemu-system-x86_64.exe",),
    )
    emulator_check = _find_emulator_check(emulator, which_fn, is_file_fn)
    unpack_bootimg = _find_aosp_host_tool("unpack_bootimg", which_fn, env, product_out, is_file_fn)
    lpunpack = _find_aosp_host_tool("lpunpack", which_fn, env, product_out, is_file_fn)
    simg2img = _find_aosp_host_tool("simg2img", which_fn, env, product_out, is_file_fn)
    avbtool = _find_aosp_host_tool("avbtool", which_fn, env, product_out, is_file_fn)

    for tool in (emulator, qemu, emulator_check, unpack_bootimg, lpunpack, simg2img, avbtool):
        if not tool.available:
            diagnostics.append(tool.error or f"{tool.name} not found")
    return Capabilities(
        host=host_info,
        emulator=emulator,
        qemu=qemu,
        emulator_check=emulator_check,
        unpack_bootimg=unpack_bootimg,
        lpunpack=lpunpack,
        simg2img=simg2img,
        avbtool=avbtool,
        diagnostics=diagnostics,
    )


def find_emulator(
    *,
    environ: Mapping[str, str],
    product_out: Path | None,
    host: HostEnvironment,
    which: WhichFn,
    is_file: Callable[[Path], bool],
) -> ToolInfo:
    os_tag = "windows" if host.is_windows else "linux"
    names = ("emulator.exe", "emulator") if host.is_windows else ("emulator",)
    searched: list[str] = []

    for name in names:
        found = which(name)
        if found:
            path = Path(found)
            return ToolInfo(name="emulator", path=path, available=True, details="PATH")
        searched.append(f"PATH:{name}")

    for root_key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        root = environ.get(root_key, "").strip()
        if not root:
            continue
        for name in names:
            candidate = Path(root) / "emulator" / name
            searched.append(str(candidate))
            if is_file(candidate):
                return ToolInfo(
                    name="emulator",
                    path=candidate,
                    available=True,
                    details=f"{root_key}/emulator/{name}",
                )

    prebuilt_rel = Path("prebuilts") / "android-emulator" / f"{os_tag}-x86_64"
    starts: list[Path] = []
    if product_out is not None:
        starts.append(Path(product_out))
    build_top = environ.get("ANDROID_BUILD_TOP", "").strip()
    if build_top:
        starts.append(Path(build_top))
    for start in starts:
        found = _walk_prebuilt_emulator(start, prebuilt_rel, names, is_file, searched)
        if found is not None:
            return ToolInfo(
                name="emulator",
                path=found,
                available=True,
                details=f"AOSP prebuilts from {start}",
            )

    return ToolInfo(
        name="emulator",
        path=None,
        available=False,
        error="emulator not found (PATH, ANDROID_SDK_ROOT, ANDROID_HOME, AOSP prebuilts)",
        details="; ".join(searched[:12]) if searched else None,
    )


def probe_accelerators(
    executable: Path | None,
    kind: str,
    *,
    runner: RunnerFn | None = None,
    emulator_check: Path | None = None,
) -> AccelProbe:
    """Interrogate the selected executable. Timeouts and failures are diagnostics."""

    run = runner or default_runner
    if executable is None:
        return AccelProbe(
            executable=None,
            kind=kind,
            reported=(),
            raw_output="",
            error=f"{kind} executable not available; accel probe skipped",
        )
    argv: list[str]
    if kind == "qemu":
        argv = [os.fspath(executable), "-accel", "help"]
    elif kind == "emulator":
        argv = [os.fspath(executable), "-help-accel"]
    elif kind == "emulator-check":
        check = emulator_check or executable
        argv = [os.fspath(check), "accel"]
    else:
        return AccelProbe(
            executable=executable,
            kind=kind,
            reported=(),
            raw_output="",
            error=f"unknown accel probe kind {kind!r}",
        )
    result = run(argv)
    blob = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.error:
        return AccelProbe(
            executable=executable,
            kind=kind,
            reported=(),
            raw_output=blob,
            error=result.error,
        )
    reported = parse_accel_help(blob)
    if kind == "emulator" and _emulator_supports_software_accel(blob) and "tcg" not in reported:
        reported = (*reported, "tcg")
    if not reported and kind == "emulator":
        help_result = run([os.fspath(executable), "-help"])
        help_blob = "\n".join(part for part in (help_result.stdout, help_result.stderr) if part)
        blob = blob + ("\n" + help_blob if help_blob else "")
        reported = parse_accel_help(blob)
        if _emulator_supports_software_accel(blob) and "tcg" not in reported:
            reported = (*reported, "tcg")
        if help_result.error and not reported:
            return AccelProbe(
                executable=executable,
                kind=kind,
                reported=(),
                raw_output=blob,
                error=help_result.error,
            )
    return AccelProbe(
        executable=executable,
        kind=kind,
        reported=reported,
        raw_output=blob,
        error=None if reported else (result.error or "accel probe produced no known accelerators"),
    )


def parse_accel_help(text: str) -> tuple[str, ...]:
    found: list[str] = []
    negative_tokens = ("not installed", "not usable", "unavailable", "disabled", "does not support")
    for raw_line in text.splitlines():
        line = raw_line.strip().lower()
        if not line:
            continue
        negative = any(token in line for token in negative_tokens)
        for name in _ACCEL_NAMES:
            if name not in line:
                continue
            if negative:
                continue
            if name not in found:
                found.append(name)
    return tuple(found)


def _emulator_supports_software_accel(text: str) -> bool:
    """Android Emulator spells its TCG mode as ``-accel off``."""

    lowered = text.lower()
    return (
        "valid values" in lowered
        and "off" in lowered
        and "disables acceleration" in lowered
    )


def select_accelerator(
    requested: str,
    host: HostEnvironment,
    probe: AccelProbe,
    *,
    executable_format_name: str | None = None,
    executable: Path | None = None,
) -> AccelSelection:
    """Choose an accelerator. Only ``auto`` may fall back to TCG with a reason."""

    name = (requested or "auto").strip().lower()
    valid = {"auto", "whpx", "kvm", "tcg"}
    if name not in valid:
        raise AcceleratorError(f"unknown accelerator {requested!r}; expected auto, whpx, kvm, or tcg")
    fmt = executable_format_name
    if fmt is None:
        fmt = executable_format(executable if executable is not None else probe.executable)
    available = tuple(probe.reported)
    avail_set = set(available)

    if name == "whpx":
        _reject_whpx_on_elf_or_wsl(host, fmt)
        if probe.error and "whpx" not in avail_set:
            raise AcceleratorError(
                f"WHPX requested but accel probe failed: {probe.error}"
            )
        if "whpx" not in avail_set:
            raise AcceleratorError(
                "WHPX requested but the selected executable did not report WHPX support"
            )
        if not host.is_windows or host.is_wsl:
            raise AcceleratorError("WHPX is only valid for native Windows executables")
        if fmt != "pe":
            raise AcceleratorError(
                "WHPX requires a Windows PE executable that reports WHPX support"
            )
        return AccelSelection(
            requested=name,
            selected="whpx",
            fallback=False,
            reason="Windows PE executable reported WHPX",
            available=available,
        )

    if name == "kvm":
        if host.is_windows and not host.is_wsl:
            raise AcceleratorError("KVM is not available on native Windows")
        if probe.error and "kvm" not in avail_set:
            raise AcceleratorError(f"KVM requested but accel probe failed: {probe.error}")
        if "kvm" not in avail_set:
            raise AcceleratorError("KVM requested but the selected executable did not report KVM")
        if not host.kvm_accessible:
            raise AcceleratorError("KVM requested but /dev/kvm is missing or not accessible")
        return AccelSelection(
            requested=name,
            selected="kvm",
            fallback=False,
            reason="/dev/kvm accessible and executable reported KVM",
            available=available,
        )

    if name == "tcg":
        if probe.error and "tcg" not in avail_set:
            raise AcceleratorError(f"TCG requested but accel probe failed: {probe.error}")
        if avail_set and "tcg" not in avail_set:
            raise AcceleratorError("TCG requested but the selected executable did not report TCG")
        return AccelSelection(
            requested=name,
            selected="tcg",
            fallback=False,
            reason="TCG requested explicitly",
            available=available,
        )

    # auto
    reasons: list[str] = []
    if probe.error:
        reasons.append(f"accel probe diagnostic: {probe.error}")
    if host.is_windows and not host.is_wsl and fmt != "elf" and "whpx" in avail_set:
        return AccelSelection(
            requested="auto",
            selected="whpx",
            fallback=False,
            reason="auto selected WHPX (Windows PE executable reported support)",
            available=available,
        )
    if host.is_linux and host.kvm_accessible and "kvm" in avail_set:
        return AccelSelection(
            requested="auto",
            selected="kvm",
            fallback=False,
            reason="auto selected KVM (/dev/kvm accessible, executable reported KVM)",
            available=available,
        )
    if host.is_linux and not host.kvm_accessible:
        reasons.append("/dev/kvm missing or not accessible")
    if "kvm" not in avail_set:
        reasons.append("executable did not report KVM")
    if "whpx" not in avail_set:
        reasons.append("WHPX not reported or not valid on this host")
    reason = "; ".join(reasons) if reasons else "hardware accel unavailable"
    reason = f"{reason}; falling back to TCG"
    if avail_set and "tcg" not in avail_set:
        raise AcceleratorError(
            f"auto could not select WHPX or KVM and TCG was not reported ({reason})"
        )
    return AccelSelection(
        requested="auto",
        selected="tcg",
        fallback=True,
        reason=reason,
        available=available,
    )


def default_runner(argv: Sequence[str]) -> ProbeResult:
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SEC,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        return ProbeResult(tuple(argv), None, "", "", f"not found: {exc}")
    except subprocess.TimeoutExpired as exc:
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        return ProbeResult(
            tuple(argv),
            None,
            stdout,
            stderr,
            f"timed out after {PROBE_TIMEOUT_SEC:.0f}s",
        )
    except OSError as exc:
        return ProbeResult(tuple(argv), None, "", "", str(exc))
    return ProbeResult(
        tuple(argv),
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
        None,
    )


def probe_emulator_help(executable: Path, *, runner: RunnerFn | None = None) -> ProbeResult:
    run = runner or default_runner
    return run([os.fspath(executable), "-help"])


def _reject_whpx_on_elf_or_wsl(host: HostEnvironment, fmt: str) -> None:
    if host.is_wsl:
        raise AcceleratorError(
            "WSL Linux qemu/emulator rejects --accel whpx; WHPX is Windows-only"
        )
    if fmt == "elf":
        raise AcceleratorError(
            "Linux/WSL ELF executable cannot use --accel whpx (WHPX is for Windows PE binaries)"
        )
    if host.is_linux and not host.is_windows:
        raise AcceleratorError(
            "WHPX is not valid on Linux; WSL Linux qemu rejects --accel whpx"
        )


def _find_aosp_host_tool(
    name: str,
    which: WhichFn,
    environ: Mapping[str, str],
    product_out: Path | None,
    is_file: Callable[[Path], bool],
) -> ToolInfo:
    found = which(name)
    if found:
        return ToolInfo(name=name, path=Path(found), available=True, details="PATH")
    host_out = environ.get("ANDROID_HOST_OUT", "").strip()
    if host_out:
        for candidate_name in (name, f"{name}.exe"):
            candidate = Path(host_out) / "bin" / candidate_name
            if is_file(candidate):
                return ToolInfo(
                    name=name,
                    path=candidate,
                    available=True,
                    details="ANDROID_HOST_OUT/bin",
                )
    rels = (
        Path("out") / "host" / "linux-x86" / "bin",
        Path("out") / "host" / "windows-x86" / "bin",
        Path("out") / "host" / "linux-x86_64" / "bin",
    )
    starts: list[Path] = []
    if product_out is not None:
        starts.append(Path(product_out))
    build_top = environ.get("ANDROID_BUILD_TOP", "").strip()
    if build_top:
        starts.append(Path(build_top))
    for start in starts:
        current = Path(start)
        for _ in range(_WALK_LIMIT):
            for rel in rels:
                bin_dir = current / rel
                for candidate_name in (name, f"{name}.exe"):
                    candidate = bin_dir / candidate_name
                    if is_file(candidate):
                        return ToolInfo(
                            name=name,
                            path=candidate,
                            available=True,
                            details=str(rel),
                        )
            parent = current.parent
            if parent == current:
                break
            current = parent
    return ToolInfo(name=name, path=None, available=False, error=f"{name} not found")


def _tool_from_which(name: str, which: WhichFn, extra_names: tuple[str, ...] = ()) -> ToolInfo:
    for candidate in (name, *extra_names):
        found = which(candidate)
        if found:
            return ToolInfo(name=name, path=Path(found), available=True, details="PATH")
    return ToolInfo(name=name, path=None, available=False, error=f"{name} not found on PATH")


def _find_emulator_check(
    emulator: ToolInfo,
    which: WhichFn,
    is_file: Callable[[Path], bool],
) -> ToolInfo:
    found = which("emulator-check") or which("emulator-check.exe")
    if found:
        return ToolInfo(name="emulator-check", path=Path(found), available=True, details="PATH")
    if emulator.path is not None:
        sibling_names = ("emulator-check", "emulator-check.exe")
        for sibling in sibling_names:
            candidate = emulator.path.parent / sibling
            if is_file(candidate):
                return ToolInfo(
                    name="emulator-check",
                    path=candidate,
                    available=True,
                    details="sibling of emulator",
                )
    return ToolInfo(
        name="emulator-check",
        path=None,
        available=False,
        error="emulator-check not found",
    )


def _walk_prebuilt_emulator(
    start: Path,
    rel: Path,
    names: tuple[str, ...],
    is_file: Callable[[Path], bool],
    searched: list[str],
) -> Path | None:
    current = start
    for _ in range(_WALK_LIMIT):
        base = current / rel
        for name in names:
            candidate = base / name
            searched.append(str(candidate))
            if is_file(candidate):
                return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
