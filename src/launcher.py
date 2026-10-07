#!/usr/bin/env python3
"""AOSP x86_64 PRODUCT_OUT launcher CLI (StartLoader)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from capabilities import Capabilities, detect_host, discover_tools  # noqa: E402
from aosp_builder import build_aosp_product  # noqa: E402
from command_builder import build_launch_plan, format_command, format_command_redacted  # noqa: E402
from errors import AmbiguousProductOutError, LauncherError  # noqa: E402
from models import LaunchOptions, LaunchPlan, ProductArtifacts  # noqa: E402
from product_out import inventory_product_out, list_product_outs, resolve_product_out  # noqa: E402


def parse_memory_mb(value: str) -> int:
    text = value.strip().replace(" ", "")
    if not text:
        raise argparse.ArgumentTypeError("memory must be an integer MB value or a size like 4G")
    upper = text.upper()
    multiplier = 1
    if upper.endswith("G"):
        multiplier = 1024
        upper = upper[:-1]
    elif upper.endswith("M"):
        multiplier = 1
        upper = upper[:-1]
    elif upper.endswith("K"):
        raise argparse.ArgumentTypeError("memory must be specified in MB or G (legacy 4G)")
    try:
        amount = int(upper, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid memory {value!r}") from exc
    mb = amount * multiplier
    if mb <= 0:
        raise argparse.ArgumentTypeError("memory must be positive")
    return mb


def default_cpus() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launcher.py",
        description="Launch an AOSP x86_64 PRODUCT_OUT via Android Emulator (ranchu) or diagnose Cuttlefish.",
    )
    parser.add_argument("--product-out", help="PRODUCT_OUT directory (overrides PRODUCT_OUT env)")
    parser.add_argument("--target", help="Select among out/target/product/<target> when multiple exist")
    build = parser.add_argument_group("AOSP source build")
    build.add_argument(
        "--build-aosp",
        type=Path,
        metavar="ROOT",
        help="Build an x86_64 Emulator product from an AOSP source root before continuing",
    )
    build.add_argument(
        "--lunch",
        default="sdk_phone64_x86_64-trunk_staging-userdebug",
        help="AOSP lunch target (default: sdk_phone64_x86_64-trunk_staging-userdebug)",
    )
    build.add_argument(
        "--build-jobs",
        type=int,
        default=4,
        metavar="N",
        help="Parallel AOSP build jobs (default: 4)",
    )
    build.add_argument(
        "--build-target",
        action="append",
        dest="build_targets",
        metavar="TARGET",
        help="AOSP m target (repeatable; default: droid)",
    )
    build.add_argument(
        "--build-only",
        action="store_true",
        help="Build and validate PRODUCT_OUT without launching the emulator",
    )
    parser.add_argument(
        "--list-product-outs",
        nargs="?",
        const=".",
        metavar="ROOT",
        help="List PRODUCT_OUT directories under ROOT (default: cwd) and exit",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "emulator", "qemu"),
        default="auto",
        help="Launch backend (default: auto)",
    )
    parser.add_argument("--kernel", type=Path, help="Override kernel path")
    parser.add_argument("--ramdisk", type=Path, help="Override ramdisk path")
    parser.add_argument("--vendor-boot", type=Path, help="Rejected: vendor_boot is never -initrd or -drive")
    parser.add_argument("--system", type=Path, help="Override system.img")
    parser.add_argument("--system-ext", type=Path, help="Override system_ext.img")
    parser.add_argument("--vendor", type=Path, help="Override vendor.img")
    parser.add_argument("--product", type=Path, dest="product_image", help="Override product.img")
    parser.add_argument("--userdata", type=Path, help="Override userdata.img source")
    parser.add_argument(
        "--memory",
        type=parse_memory_mb,
        default=4096,
        help="Guest memory in MB, or legacy size like 4G (default: 4096)",
    )
    parser.add_argument("--cpus", type=int, default=None, help="vCPU count (default: min(4, host cpus))")
    parser.add_argument("--adb-port", type=int, default=5555, help="Goldfish ADB port (default: 5555 → -ports 5554,5555)")
    parser.add_argument(
        "--accel",
        choices=("auto", "whpx", "kvm", "tcg"),
        default="auto",
        help="Accelerator (default: auto; only auto may fall back to TCG)",
    )
    parser.add_argument("--headless", action="store_true", help="Pass -no-window when supported")
    parser.add_argument("--writable-system", action="store_true")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--append", help="Extra kernel cmdline (emulator -qemu -append when supported)")
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        dest="extra_args",
        help="Extra backend arg (repeatable; requires --allow-extra-args)",
    )
    parser.add_argument("--allow-extra-args", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print plan and argv; do not spawn the guest")
    parser.add_argument("--print-command", action="store_true", help="Print the command line; do not spawn")
    parser.add_argument("--diagnose", action="store_true", help="Inspect PRODUCT_OUT; do not spawn the guest")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--gui", action="store_true", help="Open the Tk frontend")
    parser.add_argument("--work-dir", type=Path, help="Override instance state directory")
    parser.add_argument("--reset-data", action="store_true", help="Reset instance userdata under the state dir")
    return parser


def options_from_args(args: argparse.Namespace) -> LaunchOptions:
    cpus = args.cpus if args.cpus is not None else default_cpus()
    if cpus <= 0:
        raise LauncherError("--cpus must be positive")
    if not (1 <= int(args.adb_port) <= 65535):
        raise LauncherError("--adb-port must be in 1..65535")
    return LaunchOptions(
        backend=args.backend,
        kernel=args.kernel,
        ramdisk=args.ramdisk,
        vendor_boot=args.vendor_boot,
        system=args.system,
        system_ext=args.system_ext,
        vendor=args.vendor,
        product_image=args.product_image,
        userdata=args.userdata,
        memory_mb=int(args.memory),
        cpus=cpus,
        adb_port=int(args.adb_port),
        accel=args.accel,
        headless=bool(args.headless),
        writable_system=bool(args.writable_system),
        snapshot=bool(args.snapshot),
        read_only=bool(args.read_only),
        append=args.append,
        extra_args=list(args.extra_args or []),
        allow_extra_args=bool(args.allow_extra_args),
        work_dir=args.work_dir,
        reset_data=bool(args.reset_data),
        dry_run=bool(args.dry_run),
        print_command=bool(args.print_command),
        verbose=bool(args.verbose),
    )


def run_gui() -> int:
    try:
        import tkinter as tk

        import gui
    except Exception as exc:
        print(f"Failed to start GUI: {exc}", file=sys.stderr)
        return 1
    root = tk.Tk()
    gui.StartLoaderGUI(root)
    root.mainloop()
    return 0


def cmd_list_product_outs(root: str) -> int:
    found = list_product_outs(root if root not in {None, ""} else ".")
    if not found:
        print("No PRODUCT_OUT directories found.")
        return 0
    for path in found:
        print(path)
    return 0


def cmd_build_aosp(args: argparse.Namespace) -> int | None:
    """Build the requested AOSP product and select its PRODUCT_OUT."""

    if args.build_aosp is None:
        if args.build_only:
            raise LauncherError("--build-only requires --build-aosp ROOT")
        return None
    if args.gui or args.list_product_outs is not None:
        raise LauncherError("--build-aosp cannot be combined with --gui or --list-product-outs")
    result = build_aosp_product(
        args.build_aosp,
        lunch_target=args.lunch,
        targets=tuple(args.build_targets or ("droid",)),
        jobs=args.build_jobs,
    )
    args.product_out = os.fspath(result.product_out)
    args.target = None
    print(f"AOSP build complete: {result.lunch_target}")
    print(f"PRODUCT_OUT: {result.product_out}")
    if args.build_only:
        product = inventory_product_out(result.product_out)
        if product.family != "emulator_ranchu" or product.incomplete:
            raise LauncherError(
                "build completed but PRODUCT_OUT is not a complete ranchu/goldfish x86_64 product"
            )
        print("Validated complete x86_64 ranchu/goldfish PRODUCT_OUT.")
        return 0
    return None


def inspect_and_plan(args: argparse.Namespace) -> tuple[ProductArtifacts, Capabilities, LaunchPlan]:
    resolved = resolve_product_out(args.product_out, target=args.target)
    product = inventory_product_out(resolved)
    host = detect_host()
    caps = discover_tools(product_out=product.product_out, host=host)
    options = options_from_args(args)
    plan = build_launch_plan(product, options, caps)
    return product, caps, plan


def format_diagnose_report(
    product: ProductArtifacts,
    capabilities: Capabilities,
    plan: LaunchPlan,
) -> str:
    lines: list[str] = []
    lines.append("StartLoader PRODUCT_OUT diagnosis")
    lines.append(f"product_out: {product.product_out}")
    lines.append(f"requested:   {product.requested_path}")
    lines.append(f"target:      {product.target_name}")
    lines.append(f"architecture: {product.architecture}")
    lines.append(f"family:      {product.family} (confidence {product.family_confidence})")
    lines.append(f"dynamic_partitions: {product.dynamic_partitions}")
    lines.append(f"incomplete:  {product.incomplete}")
    lines.append("")
    lines.append("Artifacts:")
    for kind, path in (
        ("kernel", product.kernel),
        ("ramdisk", product.ramdisk),
        ("boot", product.boot),
        ("vendor_boot", product.vendor_boot),
        ("init_boot", product.init_boot),
        ("system", product.system),
        ("vendor", product.vendor),
        ("userdata", product.userdata),
        ("super", product.super_image),
    ):
        art = product.artifact(kind)
        status = art.status if art is not None else ("missing" if path is None else "detected")
        lines.append(f"  {kind:12} {status:12} {path if path is not None else '-'}")
    if product.vendor_boot is not None:
        lines.append("  note: vendor_boot is never passed as -initrd or -drive")
    if product.super_image is not None:
        lines.append("  note: super.img is not a regular disk")
    lines.append("")
    lines.append("Architecture evidence:")
    for item in product.architecture_evidence:
        lines.append(f"  {item.source}:{item.key}={item.value}")
    lines.append("Family evidence:")
    for item in product.family_evidence:
        lines.append(f"  {item.source}:{item.key}={item.value}")
    if product.issues:
        lines.append("Issues:")
        for issue in product.issues:
            lines.append(f"  [{issue.severity}] {issue.code}: {issue.message}")
    lines.append("")
    lines.append(f"Host: {capabilities.host.platform} cpus={capabilities.host.cpu_count} "
                 f"kvm_exists={capabilities.host.kvm_node_exists} "
                 f"kvm_accessible={capabilities.host.kvm_accessible}")
    lines.append("Tools:")
    for tool in (
        capabilities.emulator,
        capabilities.qemu,
        capabilities.emulator_check,
        capabilities.unpack_bootimg,
        capabilities.lpunpack,
        capabilities.simg2img,
        capabilities.avbtool,
    ):
        loc = str(tool.path) if tool.path else "not found"
        lines.append(f"  {tool.name:16} {loc}")
    lines.append("")
    spec = plan.spec
    lines.append(f"backend:     {spec.backend}")
    lines.append(f"bootable:     {spec.bootable}")
    lines.append(f"support:      {spec.support_state}")
    lines.append(f"accelerator:  {plan.accelerator}")
    if spec.reasons:
        lines.append("Reasons:")
        for reason in spec.reasons:
            lines.append(f"  - {reason}")
    if spec.errors:
        lines.append("Backend errors:")
        for err in spec.errors:
            lines.append(f"  - {err}")
    if spec.warnings:
        lines.append("Warnings:")
        for warn in spec.warnings:
            lines.append(f"  - {warn}")
    if product.family == "cuttlefish":
        lines.append("")
        lines.append("Cuttlefish (vsoc) is not supported by this launcher in v1.")
        lines.append("Do not convert images. Use launch_cvd from the Cuttlefish host tools.")
        lines.append("Android Emulator is refused. QEMU is fail-closed.")
    if spec.argv:
        windows = capabilities.host.is_windows and not capabilities.host.is_wsl
        lines.append("")
        lines.append("Command (not executed):")
        lines.append(format_command_redacted(spec.argv, windows=windows))
    else:
        lines.append("")
        lines.append("No executable launch plan (argv empty).")
    return "\n".join(lines) + "\n"


def cmd_diagnose(args: argparse.Namespace) -> int:
    product, caps, plan = inspect_and_plan(args)
    sys.stdout.write(format_diagnose_report(product, caps, plan))
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    product, caps, plan = inspect_and_plan(args)
    spec = plan.spec
    windows = caps.host.is_windows and not caps.host.is_wsl
    executable = spec.executable if spec.executable is not None else "-"
    print(f"backend:      {spec.backend}")
    print(f"executable:   {executable}")
    print(f"product_out:  {product.product_out}")
    print(f"architecture: {product.architecture}")
    print(f"accelerator:  {plan.accelerator}")
    print("image mapping:")
    print("  roles:")
    if spec.mapping.roles:
        for role, path in spec.mapping.roles.items():
            print(f"    {role}: {path}")
    else:
        print("    (none)")
    print("  ignored:")
    if spec.mapping.ignored:
        for path, reason in spec.mapping.ignored.items():
            print(f"    {path}: {reason}")
    else:
        print("    (none)")
    print(f"bootable:      {spec.bootable}")
    print(f"support:       {spec.support_state}")
    if spec.argv:
        for index, item in enumerate(spec.argv):
            print(f"[{index}] {item}")
        print(format_command_redacted(spec.argv, windows=windows))
    else:
        print("argv=[]")
    for warn in spec.warnings:
        print(f"warning: {warn}")
    for err in spec.errors:
        print(f"error: {err}", file=sys.stderr)
    if args.verbose:
        for reason in spec.reasons:
            print(f"reason: {reason}")
    return 0 if spec.bootable else 2


def cmd_print_command(args: argparse.Namespace) -> int:
    _product, caps, plan = inspect_and_plan(args)
    spec = plan.spec
    windows = caps.host.is_windows and not caps.host.is_wsl
    if spec.argv:
        print(format_command(spec.argv, windows=windows))
    else:
        print("")
    return 0 if spec.bootable else 2


def cmd_launch(args: argparse.Namespace) -> int:
    product, caps, plan = inspect_and_plan(args)
    spec = plan.spec
    if not spec.bootable or not spec.argv:
        print("Refusing to launch: not bootable.", file=sys.stderr)
        for err in spec.errors:
            print(f"error: {err}", file=sys.stderr)
        return 2
    if args.reset_data or args.work_dir or (product.userdata and not args.read_only):
        import userdata as userdata_mod

        instance = userdata_mod.prepare_instance(
            product,
            work_dir=args.work_dir,
            reset_data=bool(args.reset_data),
            read_only=bool(args.read_only),
        )
        if instance.userdata is not None and "-data" in spec.argv:
            idx = spec.argv.index("-data")
            spec.argv[idx + 1] = str(instance.userdata)
    windows = caps.host.is_windows and not caps.host.is_wsl
    print(format_command_redacted(spec.argv, windows=windows))
    env = os.environ.copy()
    env.update(spec.env)
    cwd = os.fspath(spec.cwd) if spec.cwd is not None else None
    try:
        completed = subprocess.run(
            spec.argv,
            env=env,
            cwd=cwd,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        print(f"executable not found: {exc}", file=sys.stderr)
        return 1
    return int(completed.returncode or 0)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        built = cmd_build_aosp(args)
        if built is not None:
            return built
        if args.gui:
            return run_gui()
        if args.list_product_outs is not None:
            return cmd_list_product_outs(args.list_product_outs)
        if args.diagnose:
            return cmd_diagnose(args)
        if args.print_command:
            return cmd_print_command(args)
        if args.dry_run:
            return cmd_dry_run(args)
        if args.product_out is None and not os.environ.get("PRODUCT_OUT") and not os.environ.get("ANDROID_PRODUCT_OUT"):
            parser.print_help()
            return 2
        return cmd_launch(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except AmbiguousProductOutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        for cand in exc.candidates:
            print(f"  candidate: {cand}", file=sys.stderr)
        return int(exc.exit_code)
    except LauncherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 2) or 2)


if __name__ == "__main__":
    sys.exit(main())
