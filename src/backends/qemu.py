"""QEMU backend for v1.

For the generic ``unknown`` x86_64 family and for ``cuttlefish``,
qemu-system-x86_64 is used when a kernel, initrd, and system.img are available
and no unsupported images (vendor_boot passed as a disk, super.img as an
ordinary disk) are present.

Cuttlefish uses the same native boot.img/vendor_boot.img/init_boot.img/super.img
parsing path as ``unknown``; it does not use crosvm/launch_cvd or any
Cuttlefish-specific devices. ranchu/goldfish remains refused because it needs
goldfish pipe/sync/battery devices the stock qemu binary does not provide.

vendor_boot is never -initrd/-drive; super.img is never an ordinary disk.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from backends.base import Backend
from bootimg import (
    BootImageError,
    _read_kernel_version,
    build_combined_initrd,
    build_partition_map_and_fstab,
    build_pci_boot_devices,
    build_virtio_modules_ramdisk,
    detect_hardware_property,
    pack_cpio_newc,
    read_boot_image,
    read_vendor_boot_image,
)
from capabilities import AccelSelection, Capabilities
from models import ArtifactMapping, CommandSpec, LaunchOptions, ProductArtifacts
from superimg import (
    SuperImageError,
    available_base_partition_names,
    available_slot_suffixes,
    extract_partition_to_file,
    read_super_metadata,
)

_ADB_DEFAULT_PORT = 5555


def _qemu_format(path: Path) -> str:
    """Return 'qcow2' for .qcow2 files, otherwise 'raw'."""
    return "qcow2" if str(path).endswith(".qcow2") else "raw"


_STANDARD_FIRMWARE_DIRS = (
    Path("/usr/share/qemu"),
    Path("/usr/local/share/qemu"),
)


def _find_aosp_root(product_out: Path | None) -> Path | None:
    """Derive the AOSP source root from a PRODUCT_OUT path.

    PRODUCT_OUT is always ``<AOSP_ROOT>/out/target/product/<target>`` in a
    standard AOSP tree, so walking up until ``build/envsetup.sh`` is found
    gives a reliable root without requiring an explicit ``--aosp-root`` flag.
    Returns ``None`` when the path is not inside a recognizable AOSP tree.
    """

    if product_out is None:
        return None
    current = Path(product_out)
    for _ in range(8):
        if (current / "build" / "envsetup.sh").is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _detect_firmware_dir(qemu_path: Path | None) -> Path | None:
    """Find a QEMU firmware/ROM directory (bios-256k.bin, linuxboot_dma.bin)
    for locally-built, not-``ninja install``-ed qemu-system-x86_64 binaries.

    A freshly built-from-source qemu that was never installed cannot find its
    BIOS/option-ROM blobs at its compiled-in default datadir, so direct
    kernel boot (``-kernel``) silently falls through to an unbootable BIOS
    with zero diagnostic output. If a standard system install already has
    the firmware, trust the binary's own compiled-in default (return None,
    no ``-L`` override needed); otherwise look for the build tree's own
    bundled firmware next to the binary and point ``-L`` at it.
    """
    if qemu_path is None:
        return None
    for standard in _STANDARD_FIRMWARE_DIRS:
        if (standard / "bios-256k.bin").is_file():
            return None
    candidates = [
        qemu_path.parent / "qemu-bundle" / "usr" / "local" / "share" / "qemu",
        qemu_path.parent / "pc-bios",
        qemu_path.parent.parent / "share" / "qemu",
        qemu_path.parent / "share" / "qemu",
    ]
    for candidate in candidates:
        if (candidate / "bios-256k.bin").is_file():
            return candidate
    return None


def _probe_netdev_backends(qemu_path: Path | None) -> frozenset[str]:
    """Return the netdev backend names this qemu-system-x86_64 was built with.

    Some distro/source builds omit libslirp, so ``-netdev user`` is not always
    available (it fails at runtime with "network backend 'user' is not
    compiled into this binary", not at argv-construction time). ``-netdev
    help`` reflects actual compile-time support, so probe it instead of
    assuming ``user`` is always present.
    """
    if qemu_path is None:
        return frozenset()
    try:
        result = subprocess.run(
            [str(qemu_path), "-netdev", "help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )


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
        if product.family == "emulator_ranchu":
            errors.append(
                "ranchu/goldfish requires Android Emulator goldfish pipe/sync/battery devices; "
                "plain qemu-system-x86_64 is unsupported"
            )

        # ── super.img / dynamic_partitions refusal (ranchu only) ────────────
        # The unknown and cuttlefish families attempt native liblp extraction
        # of dynamic partitions from super.img below; emulator_ranchu never
        # reaches that extraction path and keeps being refused outright.
        if product.dynamic_partitions or product.super_image is not None:
            if product.super_image is not None:
                ignored[str(product.super_image)] = "not an ordinary disk"
            if product.family not in ("unknown", "cuttlefish"):
                errors.append("super.img / dynamic partitions cannot be attached as an ordinary QEMU disk")

        # ── vendor_boot refusal ─────────────────────────────────────────────
        # options.vendor_boot (--vendor-boot CLI flag) is always refused
        if options.vendor_boot is not None:
            errors.append("vendor_boot must not be passed as -initrd or -drive")
            ignored[str(options.vendor_boot)] = "never -initrd or -drive"
        # product.vendor_boot is consumed by the unknown/cuttlefish native-parse path
        if product.vendor_boot is not None and product.family not in ("unknown", "cuttlefish"):
            errors.append("vendor_boot must not be passed as -initrd or -drive")
            ignored[str(product.vendor_boot)] = "never -initrd or -drive"

        # ── init_boot is ignored unless the unknown/cuttlefish family consumes it ─
        if product.init_boot is not None and product.family not in ("unknown", "cuttlefish"):
            ignored[str(product.init_boot)] = "init_boot is not used as QEMU initrd in v1"

        # ── extra_args gate (all families) ──────────────────────────────────
        if options.extra_args and not options.allow_extra_args:
            errors.append("--extra-arg requires --allow-extra-args")

        # ── early exit for ranchu or when errors already set ────────────────
        if product.family == "emulator_ranchu":
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

        if product.family not in ("unknown", "cuttlefish"):
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

        # system image required (checked early: the drive list below,
        # built from system + the optional images, is needed before kernel/
        # ramdisk resolution so the native boot.img path can inject a
        # matching androidboot.partition_map + fstab).
        system = options.system or product.system
        vendor = options.vendor or product.vendor
        product_image = options.product_image or product.product
        system_ext = options.system_ext or product.system_ext
        odm = product.optional_images.get("odm")
        vendor_dlkm = product.optional_images.get("vendor_dlkm")
        odm_dlkm = product.optional_images.get("odm_dlkm")

        # ── super.img native extraction (unknown family, dynamic partitions) ─
        # When PRODUCT_OUT has no standalone per-partition .img files but does
        # have a super.img, try extracting the missing dynamic partitions from
        # it via the native liblp parser. Each partition is resolved
        # independently: failure to extract one only warns and does not
        # block the others, or the non-super_image code paths above.
        _SUPER_DYNAMIC_PARTITIONS = (
            "system",
            "vendor",
            "product",
            "system_ext",
            "odm",
            "vendor_dlkm",
            "odm_dlkm",
        )
        slot_suffix = ""
        _super_sources: dict[str, Path | None] = {
            "system": system,
            "vendor": vendor,
            "product": product_image,
            "system_ext": system_ext,
            "odm": odm,
            "vendor_dlkm": vendor_dlkm,
            "odm_dlkm": odm_dlkm,
        }
        if product.super_image is not None and any(
            src is None for src in _super_sources.values()
        ):
            try:
                _super_meta = read_super_metadata(
                    product.super_image, simg2img=capabilities.simg2img.path
                )
            except SuperImageError as exc:
                warnings.append(f"super.img present but metadata could not be parsed: {exc}")
                _super_meta = None
            if _super_meta is not None:
                if options.slot:
                    slot_suffix = f"_{options.slot}"
                else:
                    _suffixes = available_slot_suffixes(_super_meta)
                    slot_suffix = _suffixes[0] if _suffixes else ""
                _base_names = set(available_base_partition_names(_super_meta))
                _super_tmpdir = tempfile.mkdtemp()
                for _role in _SUPER_DYNAMIC_PARTITIONS:
                    if _role not in _base_names:
                        continue
                    if _super_sources[_role] is not None:
                        continue
                    try:
                        _extracted = extract_partition_to_file(
                            product.super_image,
                            _super_meta,
                            _role,
                            slot_suffix,
                            Path(_super_tmpdir),
                            simg2img=capabilities.simg2img.path,
                        )
                        _super_sources[_role] = _extracted
                        warnings.append(
                            f"extracted '{_role}{slot_suffix}' from {product.super_image} via native liblp parsing"
                        )
                    except SuperImageError as exc:
                        warnings.append(f"could not extract '{_role}{slot_suffix}' from super.img: {exc}")
                system = _super_sources["system"]
                vendor = _super_sources["vendor"]
                product_image = _super_sources["product"]
                system_ext = _super_sources["system_ext"]
                odm = _super_sources["odm"]
                vendor_dlkm = _super_sources["vendor_dlkm"]
                odm_dlkm = _super_sources["odm_dlkm"]

        # A flat, non-A/B PRODUCT_OUT has no super.img slot info, but the
        # Boot HAL hard-requires a non-empty ro.boot.slot_suffix. Default to
        # the standard A/B slot '_a' when no suffix was discovered above.
        if not slot_suffix:
            slot_suffix = "_a"

        if system is None:
            errors.append("missing system image: provide --system or a PRODUCT_OUT with system.img")
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        drives: list[tuple[str, Path]] = [("system", system)]
        for role, src in [
            ("vendor", vendor),
            ("product", product_image),
            ("system_ext", system_ext),
            ("odm", odm),
            ("vendor_dlkm", vendor_dlkm),
            ("odm_dlkm", odm_dlkm),
            ("data", options.userdata or product.userdata),
        ]:
            if src is not None:
                drives.append((role, src))

        # ── synthetic /metadata image (Cuttlefish/generic native path) ──────
        # Real Cuttlefish PRODUCT_OUT never ships a standalone metadata.img;
        # launch_cvd synthesizes one on the fly. Do the same here so first-stage
        # init's SnapshotManager has a writable, empty /metadata partition.
        metadata_src = product.optional_images.get("metadata")
        _metadata_tmpdir: str | None = None
        if metadata_src is None:
            if capabilities.mke2fs.available and capabilities.mke2fs.path is not None:
                try:
                    _metadata_tmpdir = tempfile.mkdtemp()
                    metadata_path = Path(_metadata_tmpdir) / "metadata.img"
                    metadata_path.write_bytes(b"\x00" * (16 * 1024 * 1024))
                    subprocess.run(
                        [
                            str(capabilities.mke2fs.path),
                            "-t", "ext4",
                            "-b", "4096",
                            "-L", "metadata",
                            str(metadata_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    metadata_src = metadata_path
                    warnings.append(
                        f"synthesized empty /metadata image at {metadata_src}"
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    warnings.append(f"could not synthesize /metadata image: {exc}")
                    metadata_src = None
            else:
                warnings.append(
                    "mke2fs not found; /metadata partition will not be available"
                )
        if metadata_src is not None:
            drives.insert(0, ("metadata", metadata_src))

        # ── synthetic /misc image (Cuttlefish/generic native path) ───────────
        # Real AOSP Cuttlefish PRODUCT_OUT never ships a standalone misc.img,
        # but BootControl requires a writable /misc partition to read and write
        # the bootloader_message struct. Synthesize a small, empty raw image.
        misc_src = product.optional_images.get("misc")
        _misc_tmpdir: str | None = None
        if misc_src is None:
            try:
                _misc_tmpdir = tempfile.mkdtemp()
                misc_path = Path(_misc_tmpdir) / "misc.img"
                misc_path.write_bytes(b"\x00" * (1 * 1024 * 1024))
                misc_src = misc_path
                warnings.append(
                    f"synthesized empty /misc image at {misc_src}"
                )
            except OSError as exc:
                warnings.append(f"could not synthesize /misc image: {exc}")
                misc_src = None
        if misc_src is not None:
            # Place misc immediately after metadata (if present) so metadata
            # remains the first drive; any position works for by-name symlinks.
            insert_idx = 1 if any(r == "metadata" for r, _ in drives) else 0
            drives.insert(insert_idx, ("misc", misc_src))

        # kernel + ramdisk resolution
        kernel = options.kernel or product.kernel
        ramdisk = options.ramdisk or product.ramdisk
        _boot_cmdline: str = ""
        _tmpdir: str | None = None
        # Prefer the native boot.img parser whenever product.boot is available,
        # unless the user explicitly supplied both --kernel and --ramdisk to
        # bypass it. A real AOSP product may ship standalone kernel/ramdisk.img
        # convenience files alongside boot.img/vendor_boot.img/init_boot.img;
        # the native path is the only one that produces a combined initrd with
        # the synthetic fstab, partition_map, and virtio-module injection.
        explicit_kernel_and_ramdisk = options.kernel is not None and options.ramdisk is not None
        if product.boot is not None and not explicit_kernel_and_ramdisk:
            boot_img = product.boot
            # ── Native boot.img parser (preferred, no external tools) ────────────
            _native_tmpdir: str | None = None
            if boot_img is not None:
                try:
                    boot_sec = read_boot_image(boot_img)
                    init_sec = (
                        read_boot_image(product.init_boot)
                        if product.init_boot
                        else None
                    )
                    vend_sec = (
                        read_vendor_boot_image(product.vendor_boot)
                        if product.vendor_boot
                        else None
                    )
                    # ── by-name partition mapping + synthetic fstab ──────
                    # This flat (no GPT, no super.img) per-partition disk
                    # layout has no on-disk way for fs_mgr to discover
                    # "/dev/block/by-name/<partition>" on its own, and the
                    # vendor_boot ramdisk we just combined with typically
                    # has no static /fstab.<hardware> file either (it
                    # normally expects a device-tree-provided fstab that
                    # only Cuttlefish's own crosvm/launch_cvd supplies).
                    # androidboot.partition_map creates the by-name
                    # symlinks from virtio-blk device name -> partition
                    # name, and injecting our own /fstab.qemu gives
                    # first-stage mount somewhere to find the resulting
                    # mount table. androidboot.force_normal_boot=1 is also
                    # required: many generic/GKI ramdisks bundle a combined
                    # normal+recovery init and default to recovery mode
                    # whenever /system/bin/recovery exists in that ramdisk.
                    role_names = [role for role, _ in drives]
                    hardware_name = detect_hardware_property(
                        vend_sec, options.append or ""
                    )
                    partition_map, fstab_name, fstab_text = build_partition_map_and_fstab(
                        role_names, hardware=hardware_name
                    )
                    boot_devices, pci_slots = build_pci_boot_devices(len(drives))
                    # drives[i] gets PCI slot pci_slots[i] — the drive-building
                    # section below (-device virtio-blk-pci,addr=...) must use
                    # the exact same slot assignment for this to be correct.
                    # Some vendor ramdisks (e.g. GKI "first_stage_ramdisk" layout)
                    # switch_root into a "/first_stage_ramdisk" subtree before
                    # first-stage mount even looks for the fstab; place the
                    # synthetic fstab at both the true root and inside that
                    # subtree so it is found either way.
                    fstab_bytes = fstab_text.encode("utf-8")
                    fstab_cpio = pack_cpio_newc([
                        (fstab_name, fstab_bytes),
                        (f"first_stage_ramdisk/{fstab_name}", fstab_bytes),
                        (f"system/etc/{fstab_name}", fstab_bytes),
                        (f"first_stage_ramdisk/system/etc/{fstab_name}", fstab_bytes),
                        (f"vendor/etc/{fstab_name}", fstab_bytes),
                        (f"first_stage_ramdisk/vendor/etc/{fstab_name}", fstab_bytes),
                    ])

                    # GKI-style kernels often ship virtio-blk/PCI as loadable
                    # modules. If we can locate the matching AOSP prebuilt
                    # module tree, splice those modules into the generic
                    # ramdisk so first-stage init can load them before it
                    # tries to mount by-name partitions.
                    extra_ramdisk = fstab_cpio
                    if boot_sec.kernel:
                        kernel_version = _read_kernel_version(boot_sec.kernel)
                        aosp_root = options.aosp_root or _find_aosp_root(product.product_out)
                        if kernel_version and aosp_root:
                            modules_ramdisk, mod_warning = build_virtio_modules_ramdisk(
                                aosp_root,
                                kernel_version,
                                architecture=product.architecture or "x86_64",
                            )
                            if mod_warning:
                                warnings.append(mod_warning)
                            if modules_ramdisk:
                                extra_ramdisk = extra_ramdisk + modules_ramdisk

                    combined_bytes, boot_cmdline = build_combined_initrd(
                        boot_sec,
                        init_sec,
                        vend_sec,
                        extra_ramdisk=extra_ramdisk,
                        lz4_path=str(capabilities.lz4.path) if capabilities.lz4.available else None,
                    )
                    boot_cmdline = " ".join(
                        s for s in [
                            boot_cmdline,
                            "androidboot.force_normal_boot=1",
                            f"androidboot.partition_map={partition_map}",
                            f"androidboot.boot_devices={boot_devices}",
                            f"androidboot.slot_suffix={slot_suffix}",
                        ] if s.strip()
                    )

                    _native_tmpdir = tempfile.mkdtemp()
                    kernel_tmp = Path(_native_tmpdir) / "kernel"
                    ramdisk_tmp = Path(_native_tmpdir) / "ramdisk"
                    assert boot_sec.kernel is not None
                    kernel_tmp.write_bytes(boot_sec.kernel)
                    ramdisk_tmp.write_bytes(combined_bytes)
                    # Prefer the native-parsed result over the plain
                    # product.kernel/product.ramdisk fallback, but respect an
                    # explicit --kernel or --ramdisk option if only one was
                    # supplied (the full explicit-override escape hatch skips
                    # this block entirely when both are supplied).
                    kernel = kernel_tmp if options.kernel is None else kernel
                    ramdisk = ramdisk_tmp if options.ramdisk is None else ramdisk
                    _boot_cmdline = boot_cmdline
                except Exception:
                    pass
            # ── Existing unpack_bootimg fallback (kept as last resort) ───────────
            if (
                kernel is None or ramdisk is None
            ) and boot_img is not None and capabilities.unpack_bootimg.available:
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

        # A product with no boot.img at all may still be missing kernel or
        # ramdisk after the fallback above; catch that case here.
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

        # if we already have cross-family errors (vendor_boot/super.img/extra_args), bail
        if errors:
            return _fail(self, capabilities, errors, warnings, ignored, reasons)

        # build argv
        exe = str(capabilities.qemu.path)
        argv = [exe]

        firmware_dir = _detect_firmware_dir(capabilities.qemu.path)
        if firmware_dir is not None:
            argv += ["-L", str(firmware_dir)]
            warnings.append(
                f"using bundled firmware at {firmware_dir} "
                "(qemu-system-x86_64 appears to be built but not installed; "
                "run 'ninja install' in its build directory to avoid this)"
            )

        # accel
        argv += ["-accel", accel.selected]
        if accel.fallback:
            warnings.append(f"accelerator fallback to {accel.selected}: {accel.reason}")

        # machine / memory / cpus / cpu model
        argv += ["-machine", "pc"]
        # 'max' exposes the full host/TCG feature set (SSE4.2/SSSE3/AVX ...),
        # which AOSP x86_64 init/bionic require; skipped if the user already
        # supplies their own -cpu via --extra-arg (applied later, last-wins).
        if not (options.allow_extra_args and "-cpu" in options.extra_args):
            argv += ["-cpu", "max"]
        argv += ["-m", str(options.memory_mb)]
        argv += ["-smp", str(options.cpus)]

        # kernel + initrd + cmdline
        argv += ["-kernel", str(kernel)]
        argv += ["-initrd", str(ramdisk)]
        cmdline_base = "console=ttyS0 androidboot.hardware=qemu"
        parts = [s for s in [_boot_cmdline, cmdline_base] if s.strip()]
        cmdline = " ".join(parts)
        if options.append:
            cmdline += " " + options.append
        argv += ["-append", cmdline]

        # drives: the precomputed `drives` list (system first, then any of
        # vendor/product/system_ext/data that are present) — same order the
        # native boot.img path used to build androidboot.partition_map, so
        # the virtio-blk device naming (vda, vdb, ...) the guest kernel
        # assigns actually matches what we told it. Each drive also gets an
        # explicit, fixed PCI slot (matching androidboot.boot_devices) —
        # fs_mgr/init only creates a by-name symlink for a partition_map
        # entry when the device's sysfs PCI path is listed in
        # androidboot.boot_devices (confirmed by reading
        # system/core/init/devices.cpp); relying on whatever slot QEMU
        # would auto-assign to an implicit "-drive if=virtio" makes that
        # value unknowable in advance, so assign it ourselves instead.
        _unused_boot_devices, pci_slots = build_pci_boot_devices(len(drives))
        roles: dict[str, str] = {}
        for index, (role, src) in enumerate(drives):
            fmt = _qemu_format(src)
            drive_id = f"drive{index}"
            drv = f"file={src},format={fmt},if=none,id={drive_id}"
            if role == "system" and options.writable_system:
                pass
            elif role in ("data", "metadata", "misc") and not options.read_only:
                pass
            else:
                drv += ",readonly=on"
            argv += ["-drive", drv]
            argv += [
                "-device",
                f"virtio-blk-pci,drive={drive_id},addr=0x{pci_slots[index]:x}",
            ]
            roles[role] = str(src)

        # networking: prefer user-mode slirp + hostfwd for ADB, but this
        # qemu binary may have been built without libslirp (-netdev user is
        # then rejected at runtime, not at argv time). Probe compiled-in
        # backends first and degrade gracefully instead of emitting an argv
        # that is guaranteed to fail to start.
        adb_port = int(options.adb_port)
        netdev_backends = _probe_netdev_backends(capabilities.qemu.path)
        if "user" in netdev_backends:
            argv += [
                "-netdev", f"user,id=net0,hostfwd=tcp::{adb_port}-:5555",
                "-device", "virtio-net-pci,netdev=net0",
            ]
        elif "passt" in netdev_backends and shutil.which("passt") is not None:
            # qemu's "-netdev help" lists backend *types* it was compiled
            # with, but "passt" also needs the external passt(1) helper
            # binary on PATH at runtime (qemu execs it); "help" listing it
            # does not guarantee that binary is installed.
            argv += [
                "-netdev", "passt,id=net0",
                "-device", "virtio-net-pci,netdev=net0",
            ]
            warnings.append(
                "qemu-system-x86_64 lacks the 'user' (slirp) netdev backend; "
                "using 'passt' instead, but ADB hostfwd port mapping is not "
                "configured for passt in v1 — ADB will not be reachable from the host"
            )
        else:
            argv += ["-nic", "none"]
            warnings.append(
                "qemu-system-x86_64 has no usable netdev backend ('user' or 'passt' "
                "not compiled in); networking disabled, ADB will not be reachable"
            )

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
