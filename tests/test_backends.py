"""Backend planning tests. Emulator -help is mocked; no real qemu spawn."""

from __future__ import annotations

import dataclasses
import re
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import support  # noqa: F401
from support import (
    _make_boot_img_v4,
    _make_vendor_boot_img_v4,
    make_boot_img_product,
    make_cuttlefish_product,
    make_generic_x86_64_product,
    make_incomplete_ranchu,
    make_ranchu_product,
    write_bytes,
    write_sparse,
    write_text,
    write_vendor_boot,
)

from backends.android_emulator import AndroidEmulatorBackend
from backends.qemu import QemuBackend
from capabilities import AccelSelection, Capabilities, HostEnvironment, ToolInfo
from command_builder import build_launch_plan
from models import LaunchOptions
from product_out import inventory_product_out

EMULATOR_HELP = """
Android Emulator usage:
  -sysdir <dir>
  -kernel <file>
  -ramdisk <file>
  -system <file>
  -vendor <file>
  -data <file>
  -memory <integer>
  -cores <count>
  -accel <accel>
  -no-window
  -ports <console>,<adb>
  -writable-system
  -read-only
  -snapshot
  -no-snapshot
  -qemu
"""


def _host_linux() -> HostEnvironment:
    return HostEnvironment(
        platform="linux",
        is_windows=False,
        is_linux=True,
        is_wsl=False,
        kvm_node_exists=True,
        kvm_accessible=True,
        cpu_count=4,
    )


def _missing_tool(name: str) -> ToolInfo:
    return ToolInfo(name=name, path=None, available=False, error=f"{name} not found")


def _caps(
    *,
    emulator: Path | None,
    qemu: Path | None = None,
    launch_cvd: Path | None = None,
    mke2fs: Path | None = None,
    fsck_erofs: Path | None = None,
    mkfs_erofs: Path | None = None,
    build_image: Path | None = None,
    simg2img: Path | None = None,
    kvm_accessible: bool = True,
) -> Capabilities:
    emu = (
        ToolInfo(name="emulator", path=emulator, available=emulator is not None, details="mock")
        if emulator is not None
        else _missing_tool("emulator")
    )
    qemu_tool = (
        ToolInfo(name="qemu-system-x86_64", path=qemu, available=True, details="mock")
        if qemu is not None
        else _missing_tool("qemu-system-x86_64")
    )
    launch_cvd_tool = (
        ToolInfo(name="launch_cvd", path=launch_cvd, available=True, details="mock")
        if launch_cvd is not None
        else _missing_tool("launch_cvd")
    )
    mke2fs_tool = (
        ToolInfo(name="mke2fs", path=mke2fs, available=True, details="mock")
        if mke2fs is not None
        else _missing_tool("mke2fs")
    )
    fsck_erofs_tool = (
        ToolInfo(name="fsck.erofs", path=fsck_erofs, available=True, details="mock")
        if fsck_erofs is not None
        else _missing_tool("fsck.erofs")
    )
    mkfs_erofs_tool = (
        ToolInfo(name="mkfs.erofs", path=mkfs_erofs, available=True, details="mock")
        if mkfs_erofs is not None
        else _missing_tool("mkfs.erofs")
    )
    build_image_tool = (
        ToolInfo(name="build_image", path=build_image, available=True, details="mock")
        if build_image is not None
        else _missing_tool("build_image")
    )
    simg2img_tool = (
        ToolInfo(name="simg2img", path=simg2img, available=True, details="mock")
        if simg2img is not None
        else _missing_tool("simg2img")
    )
    host = _host_linux() if kvm_accessible else dataclasses.replace(_host_linux(), kvm_accessible=False)
    return Capabilities(
        host=host,
        emulator=emu,
        qemu=qemu_tool,
        emulator_check=_missing_tool("emulator-check"),
        unpack_bootimg=_missing_tool("unpack_bootimg"),
        lpunpack=_missing_tool("lpunpack"),
        simg2img=simg2img_tool,
        avbtool=_missing_tool("avbtool"),
        lz4=_missing_tool("lz4"),
        mke2fs=mke2fs_tool,
        fsck_erofs=fsck_erofs_tool,
        mkfs_erofs=mkfs_erofs_tool,
        build_image=build_image_tool,
        launch_cvd=launch_cvd_tool,
    )


def _tcg() -> AccelSelection:
    return AccelSelection(
        requested="auto",
        selected="tcg",
        fallback=True,
        reason="test mock; falling back to TCG",
        available=("tcg",),
    )


def _whpx() -> AccelSelection:
    return AccelSelection(
        requested="whpx",
        selected="whpx",
        fallback=False,
        reason="test mock WHPX",
        available=("whpx", "tcg"),
    )


class EmulatorBackendTests(unittest.TestCase):
    def test_whpx_accel_in_emulator_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_ranchu_product(Path(raw), "emu64x")
            product = inventory_product_out(product_dir)
            emulator_bin = Path(raw) / "emulator"
            emulator_bin.write_text("#!/bin/sh\n")
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(backend="emulator", accel="whpx", adb_port=5555, headless=True),
                _caps(emulator=emulator_bin),
                accel=_whpx(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertTrue(spec.bootable)
            argv = spec.argv
            self.assertIn("-accel", argv)
            self.assertEqual(argv[argv.index("-accel") + 1], "on")
            joined = " ".join(argv)
            self.assertNotIn("hostfwd", joined)
            self.assertNotIn("netdev", joined)

    def test_ranchu_builds_emulator_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_ranchu_product(Path(raw), "emu64x")
            product = inventory_product_out(product_dir)
            emulator_bin = Path(raw) / "emulator"
            emulator_bin.write_text("#!/bin/sh\n")
            caps = _caps(emulator=emulator_bin)
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(backend="emulator", accel="tcg", adb_port=5555, headless=True),
                caps,
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertTrue(spec.bootable)
            self.assertEqual(spec.support_state, "supported")
            argv = spec.argv
            self.assertEqual(argv[0], str(emulator_bin))
            self.assertIn("-sysdir", argv)
            self.assertIn("-kernel", argv)
            self.assertIn("-ramdisk", argv)
            self.assertIn("-system", argv)
            self.assertIn("-vendor", argv)
            self.assertIn("-data", argv)
            self.assertIn("-memory", argv)
            self.assertIn("-cores", argv)
            self.assertIn("-accel", argv)
            self.assertEqual(argv[argv.index("-accel") + 1], "off")
            self.assertIn("-no-window", argv)
            self.assertIn("-ports", argv)
            self.assertEqual(argv[argv.index("-ports") + 1], "5554,5555")
            joined = " ".join(argv)
            self.assertNotIn("hostfwd", joined)
            self.assertNotIn("netdev", joined)
            self.assertNotIn("vendor_boot", joined)
            self.assertNotIn("super.img", joined)

    def test_cuttlefish_not_emulator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(),
                _caps(emulator=Path(raw) / "emulator"),
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])
            self.assertTrue(any("launch_cvd" in err for err in spec.errors))

    def test_incomplete_ranchu_not_bootable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_incomplete_ranchu(Path(raw)))
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(),
                _caps(emulator=Path(raw) / "emulator"),
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])

    def test_extra_arg_requires_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_ranchu_product(Path(raw)))
            emulator_bin = Path(raw) / "emulator"
            emulator_bin.write_text("#!/bin/sh\n")
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(extra_args=["-gpu", "off"]),
                _caps(emulator=emulator_bin),
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(spec.bootable)
            self.assertTrue(any("allow-extra-args" in err for err in spec.errors))
            allowed = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(extra_args=["-gpu", "off"], allow_extra_args=True),
                _caps(emulator=emulator_bin),
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertTrue(allowed.bootable)
            self.assertEqual(allowed.argv[-2:], ["-gpu", "off"])
            self.assertTrue(allowed.extra_args_applied)

    def test_port_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_ranchu_product(Path(raw)))
            emulator_bin = Path(raw) / "emulator"
            emulator_bin.write_text("#!/bin/sh\n")
            spec = AndroidEmulatorBackend().plan(
                product,
                LaunchOptions(adb_port=5555),
                _caps(emulator=emulator_bin),
                accel=_tcg(),
                help_text=EMULATOR_HELP,
                port_in_use=lambda port: port == 5555,
            )
            self.assertFalse(spec.bootable)
            self.assertTrue(any("ports in use" in err for err in spec.errors))
            self.assertTrue(any("hostfwd" in err for err in spec.errors))


class QemuBackendTests(unittest.TestCase):
    def test_cuttlefish_no_longer_family_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None), accel=_tcg()
            )
            self.assertEqual(spec.argv, [])
            self.assertFalse(spec.bootable)
            joined = " ".join(spec.errors).lower()
            self.assertNotIn("launch_cvd", joined)
            self.assertNotIn("vendor_boot", joined)
            self.assertNotIn("cannot be attached as an ordinary qemu disk", joined)
            self.assertTrue(any("qemu" in e.lower() for e in spec.errors))

    def test_fail_closed_ranchu(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_ranchu_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None), accel=_tcg()
            )
            self.assertEqual(spec.argv, [])
            self.assertFalse(spec.bootable)
            self.assertTrue(any("goldfish" in err.lower() or "ranchu" in err.lower() for err in spec.errors))

    def test_never_maps_vendor_boot_or_super(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None), accel=_tcg()
            )
            blob = " ".join(spec.argv)
            self.assertNotIn("vendor_boot", blob)
            self.assertNotIn("super.img", blob)
            self.assertFalse(spec.bootable)

    def test_cuttlefish_with_complete_evidence_is_bootable(self) -> None:
        """A cuttlefish-family product with the same evidence as a bootable
        unknown product is now routed through the same native-parsing path."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_boot_img_product(Path(raw), "cuttlefish_x86_64")
            product = inventory_product_out(product_dir)
            product.family = "cuttlefish"
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text("#!/bin/sh\n")
            qemu_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            self.assertIn("-kernel", spec.argv)
            self.assertIn("-initrd", spec.argv)
            self.assertNotIn("vendor_boot", " ".join(spec.argv))

    def test_cuttlefish_prefers_native_boot_path_over_standalone_kernel(self) -> None:
        """A real AOSP Cuttlefish PRODUCT_OUT ships both standalone
        kernel/ramdisk.img convenience files and proper boot*.img images. The
        native boot.img parser must be preferred so the synthetic fstab,
        partition_map, and virtio-module injection actually run."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_boot_img_product(
                Path(raw), "cuttlefish_x86_64", with_kernel=True, with_ramdisk=True
            )
            product = inventory_product_out(product_dir)
            product.family = "cuttlefish"
            assert product.kernel is not None
            assert product.ramdisk is not None
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text("#!/bin/sh\n")
            qemu_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            kernel_idx = spec.argv.index("-kernel") + 1
            initrd_idx = spec.argv.index("-initrd") + 1
            # Native path writes kernel/initrd into a tempfile, not product_dir
            self.assertNotIn(str(product_dir), spec.argv[kernel_idx])
            self.assertNotIn(str(product_dir), spec.argv[initrd_idx])
            self.assertNotEqual(Path(spec.argv[initrd_idx]), product.ramdisk)
            # The native path injects androidboot.partition_map into the cmdline
            cmdline = " ".join(spec.argv)
            self.assertIn("androidboot.partition_map", cmdline)
            self.assertNotIn(str(product.vendor_boot), cmdline)
            self.assertNotIn(str(product.init_boot), cmdline)

    def test_explicit_kernel_and_ramdisk_bypasses_native_boot_path(self) -> None:
        """A user who passes both --kernel and --ramdisk explicitly must still
        bypass boot.img parsing entirely."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_boot_img_product(
                Path(raw), "cuttlefish_x86_64", with_kernel=True, with_ramdisk=True
            )
            product = inventory_product_out(product_dir)
            product.family = "cuttlefish"
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text("#!/bin/sh\n")
            qemu_bin.chmod(0o755)
            explicit_kernel = Path(raw) / "mykernel"
            explicit_ramdisk = Path(raw) / "myramdisk"
            explicit_kernel.write_bytes(b"EKERNEL")
            explicit_ramdisk.write_bytes(b"ERAMDISK")
            spec = QemuBackend().plan(
                product,
                LaunchOptions(
                    backend="qemu",
                    kernel=explicit_kernel,
                    ramdisk=explicit_ramdisk,
                ),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            kernel_idx = spec.argv.index("-kernel") + 1
            initrd_idx = spec.argv.index("-initrd") + 1
            self.assertEqual(Path(spec.argv[kernel_idx]), explicit_kernel)
            self.assertEqual(Path(spec.argv[initrd_idx]), explicit_ramdisk)
            # Bypass means no synthetic partition_map/fstab cmdline injection
            self.assertNotIn("androidboot.partition_map", " ".join(spec.argv))

    def test_emulator_ranchu_still_refused(self) -> None:
        """ranchu/goldfish must keep being refused by the QEMU backend."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_ranchu_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None), accel=_tcg()
            )
            self.assertEqual(spec.argv, [])
            self.assertFalse(spec.bootable)
            joined = " ".join(spec.errors).lower()
            self.assertTrue(any("goldfish" in err.lower() or "ranchu" in err.lower() for err in spec.errors))
            self.assertTrue(any("missing complete kernel/initrd/cmdline evidence" in e for e in spec.errors))


def _make_aosp_virtio_module_tree(root: Path, version: str) -> Path:
    """Create a fake AOSP module tree with storage and virtio-gpu modules."""

    mod_dir = (
        root
        / "kernel"
        / "prebuilts"
        / "5.10"
        / "x86_64"
        / "system_dlkm_staging"
        / "lib"
        / "modules"
        / version
    )
    mod_dir.mkdir(parents=True)
    (mod_dir / "kernel" / "drivers" / "virtio").mkdir(parents=True)
    (mod_dir / "kernel" / "drivers" / "block").mkdir(parents=True)
    (mod_dir / "kernel" / "drivers" / "virtio" / "virtio_pci_legacy_dev.ko").write_bytes(b"MOD_LEGACY")
    (mod_dir / "kernel" / "drivers" / "virtio" / "virtio_pci_modern_dev.ko").write_bytes(b"MOD_MODERN")
    (mod_dir / "kernel" / "drivers" / "virtio" / "virtio_pci.ko").write_bytes(b"MOD_PCI")
    (mod_dir / "kernel" / "drivers" / "block" / "virtio_blk.ko").write_bytes(b"MOD_BLK")
    (mod_dir / "modules.dep").write_text(
        "kernel/drivers/virtio/virtio_pci_legacy_dev.ko:\n"
        "kernel/drivers/virtio/virtio_pci_modern_dev.ko:\n"
        "kernel/drivers/virtio/virtio_pci.ko: "
        "kernel/drivers/virtio/virtio_pci_legacy_dev.ko "
        "kernel/drivers/virtio/virtio_pci_modern_dev.ko\n"
        "kernel/drivers/block/virtio_blk.ko: kernel/drivers/virtio/virtio_pci.ko\n"
    )
    (mod_dir / "modules.alias").write_text("alias virtio_pci virtio_pci\n")
    (mod_dir / "modules.softdep").write_text("softdep data\n")

    gpu_dir = (
        root
        / "kernel"
        / "prebuilts"
        / "common-modules"
        / "virtual-device"
        # common-modules tree is laid out by major.minor kernel version
        / (re.match(r"^(\d+\.\d+)", version).group(1) if re.match(r"^(\d+\.\d+)", version) else version)
        / "x86-64"
    )
    gpu_dir.mkdir(parents=True)
    (gpu_dir / "virtio_dma_buf.ko").write_bytes(b"MOD_GPU_DMA")
    (gpu_dir / "virtio-gpu.ko").write_bytes(b"MOD_GPU")
    return root


def _make_boot_img_product_with_kernel_version(root: Path, version: str) -> Path:
    """Like ``support.make_boot_img_product`` but with a real kernel banner and a cpio init_boot ramdisk."""

    from bootimg import pack_cpio_newc

    product = root / "boot_img_x86_64"
    product.mkdir(parents=True, exist_ok=True)
    kernel_data = f"Linux version {version}\x00".encode("utf-8")
    generic_cpio = pack_cpio_newc([("placeholder", b"GENERIC")])
    write_bytes(
        product / "boot.img",
        _make_boot_img_v4(kernel_data=kernel_data, ramdisk_data=b""),
    )
    write_bytes(
        product / "init_boot.img",
        _make_boot_img_v4(kernel_data=b"", ramdisk_data=generic_cpio),
    )
    write_bytes(
        product / "vendor_boot.img",
        _make_vendor_boot_img_v4(ramdisk_data=b"\x00" * 256, bootconfig_data=b"\x00" * 32),
    )
    write_sparse(product / "system.img")
    write_sparse(product / "vendor.img")
    write_bytes(product / "userdata.img", b"\x00" * 64)
    write_text(
        product / "build.prop",
        "ro.product.cpu.abi=x86_64\nro.product.name=boot_img_x86_64\n",
    )
    return product


class GenericQemuBackendTests(unittest.TestCase):
    """Tests for the generic x86_64 QEMU boot path (product.family == 'unknown')."""

    def _make_qemu_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "qemu-system-x86_64"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_complete_generic_product_is_bootable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)
            self.assertIn("-kernel", spec.argv)
            self.assertIn("-initrd", spec.argv)
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            self.assertTrue(any("system" in v for v in drive_values))
            self.assertIn("-accel", spec.argv)
            self.assertNotIn("vendor_boot", " ".join(spec.argv))
            self.assertNotIn("super.img", " ".join(spec.argv))
            self.assertEqual(spec.errors, [])

    def test_cpu_max_is_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)
            argv = spec.argv
            self.assertIn("-cpu", argv)
            self.assertEqual(argv[argv.index("-cpu") + 1], "max")

    def test_cpu_default_skipped_when_user_supplies_cpu_extra_arg(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu", extra_args=["-cpu", "host"], allow_extra_args=True),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            cpu_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-cpu"]
            self.assertEqual(cpu_values, ["host"])

    def test_virtio_gpu_pci_device_added_after_highest_drive_slot(self) -> None:
        """A 2D virtio-gpu PCI device must be present above all virtio-blk
        drive slots so the guest kernel gets a DRM node for gralloc/minigbm
        without the GPU being treated as a boot device."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_slots: list[int] = []
            gpu_device: str | None = None
            for i, arg in enumerate(spec.argv):
                if arg != "-device":
                    continue
                value = spec.argv[i + 1]
                if "virtio-blk-pci" in value:
                    for part in value.split(","):
                        if part.startswith("addr="):
                            drive_slots.append(int(part[5:], 16))
                elif "virtio-gpu-pci" in value:
                    gpu_device = value
            self.assertIsNotNone(gpu_device)
            assert gpu_device is not None
            gpu_slot_parts = [p for p in gpu_device.split(",") if p.startswith("addr=")]
            self.assertEqual(len(gpu_slot_parts), 1)
            gpu_slot = int(gpu_slot_parts[0][5:], 16)
            self.assertGreater(gpu_slot, max(drive_slots))
            # The GPU is not a block device and must not appear in the
            # kernel cmdline (only the -append string is checked here).
            cmdline = spec.argv[spec.argv.index("-append") + 1]
            self.assertNotIn("virtio-gpu", cmdline)

    def test_vhost_vsock_pci_device_added_after_gpu_slot(self) -> None:
        """A vhost-vsock PCI device must be present above the virtio-gpu slot
        so the Cuttlefish Lights HAL can bind its AF_VSOCK listener without
        aborting, and it must not be treated as a boot device."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_slots: list[int] = []
            gpu_slot: int | None = None
            vsock_device: str | None = None
            for i, arg in enumerate(spec.argv):
                if arg != "-device":
                    continue
                value = spec.argv[i + 1]
                if "virtio-blk-pci" in value:
                    for part in value.split(","):
                        if part.startswith("addr="):
                            drive_slots.append(int(part[5:], 16))
                elif "virtio-gpu-pci" in value:
                    for part in value.split(","):
                        if part.startswith("addr="):
                            gpu_slot = int(part[5:], 16)
                elif "vhost-vsock-pci" in value:
                    vsock_device = value
            self.assertIsNotNone(vsock_device)
            assert vsock_device is not None
            self.assertIn("guest-cid=3", vsock_device)
            slot_parts = [p for p in vsock_device.split(",") if p.startswith("addr=")]
            self.assertEqual(len(slot_parts), 1)
            vsock_slot = int(slot_parts[0][5:], 16)
            self.assertIsNotNone(gpu_slot)
            assert gpu_slot is not None
            self.assertGreater(vsock_slot, gpu_slot)
            self.assertGreater(vsock_slot, max(drive_slots))
            # vsock is not a block device: must not appear in the kernel
            # cmdline (only the -append string is checked here).
            cmdline = spec.argv[spec.argv.index("-append") + 1]
            self.assertNotIn("vhost-vsock", cmdline)

    def test_data_drive_is_writable_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertNotIn("readonly=on", data_drives[0])
            system_drives = [v for v in drive_values if "system" in v]
            self.assertEqual(len(system_drives), 1)
            self.assertIn("readonly=on", system_drives[0])

    def test_data_drive_becomes_read_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu", read_only=True),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertIn("readonly=on", data_drives[0])

    def test_missing_qemu_executable_not_bootable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=None), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])
            self.assertTrue(any("qemu" in e.lower() or "executable" in e.lower() for e in spec.errors))

    def test_missing_kernel_not_bootable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            # Override with nonexistent kernel path
            spec = QemuBackend().plan(
                dataclasses.replace(product, kernel=None),
                LaunchOptions(backend="qemu", kernel=None),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])

    def test_missing_system_not_bootable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                dataclasses.replace(product, system=None),
                LaunchOptions(backend="qemu", system=None),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])

    def test_vendor_boot_on_generic_family_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            vb = Path(raw) / "vendor_boot.img"
            write_vendor_boot(vb)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu", vendor_boot=vb),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])
            self.assertNotIn("vendor_boot", " ".join(spec.argv))

    def test_dynamic_partitions_flag_alone_does_not_block_standalone_images(self) -> None:
        """The dynamic_partitions warning flag by itself (no super.img file,
        e.g. from a use_dynamic_partitions=true build property) must not
        refuse a product that otherwise has ordinary standalone
        system.img/vendor.img files to boot from."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                dataclasses.replace(product, dynamic_partitions=True),
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)

    def test_super_img_on_cuttlefish_attempts_native_extraction(self) -> None:
        """cuttlefish now attempts native liblp extraction from super.img;
        when extraction fails and no standalone system.img is present, the
        plan fails closed with a missing-system error rather than a family
        refusal."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            super_path = Path(raw) / product.target_name / "super.img"
            super_path.parent.mkdir(parents=True, exist_ok=True)
            super_path.write_bytes(b"\x00" * 4096)
            spec = QemuBackend().plan(
                dataclasses.replace(product, dynamic_partitions=True, super_image=super_path),
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])
            self.assertTrue(
                any("missing system image" in e for e in spec.errors)
            )
            self.assertFalse(
                any("cannot be attached as an ordinary QEMU disk" in e for e in spec.errors)
            )

    def test_super_img_only_extracts_system_and_vendor(self) -> None:
        """A product with a real liblp super.img and no standalone
        system.img/vendor.img must boot by extracting system/vendor from
        the super.img via the native liblp parser."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_superimg import _build_super_image_raw

        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw))
            (product_dir / "system.img").unlink()
            super_data = _build_super_image_raw(
                {"system_a": b"s" * 4096, "vendor_a": b"v" * 4096}
            )
            (product_dir / "super.img").write_bytes(super_data)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)
            self.assertTrue(
                any("extracted \'system_a\'" in w for w in spec.warnings)
            )
            self.assertTrue(
                any("extracted \'vendor_a\'" in w for w in spec.warnings)
            )

    def test_super_img_extracts_all_dynamic_partitions(self) -> None:
        """A product with a multi-partition super.img must extract every
        dynamic partition it contains (system, vendor, product, system_ext,
        odm, vendor_dlkm, odm_dlkm) and attach them as drives."""
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_superimg import _build_super_image_raw

        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw))
            for name in ("system.img", "vendor.img"):
                path = product_dir / name
                if path.exists():
                    path.unlink()
            super_data = _build_super_image_raw({
                "system_a": b"system" * 512,
                "vendor_a": b"vendor" * 512,
                "product_a": b"product" * 512,
                "system_ext_a": b"systemext" * 512,
                "odm_a": b"odm" * 1024,
                "vendor_dlkm_a": b"vendordlkm" * 512,
                "odm_dlkm_a": b"odmdlkm" * 512,
            })
            (product_dir / "super.img").write_bytes(super_data)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            for role in (
                "system", "vendor", "product", "system_ext",
                "odm", "vendor_dlkm", "odm_dlkm",
            ):
                self.assertTrue(
                    any(f"extracted \'{role}_a\'" in w for w in spec.warnings),
                    msg=f"missing extraction warning for {role}",
                )
                self.assertIn(role, spec.mapping.roles)
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            for role in ("vendor", "product", "system_ext", "odm", "vendor_dlkm", "odm_dlkm"):
                self.assertTrue(
                    any(f"{role}.img" in v for v in drive_values),
                    msg=f"missing -drive entry for {role}",
                )

    def test_unparseable_super_img_without_standalone_images_fails_closed(self) -> None:
        """A super.img that cannot be parsed, combined with no standalone
        system.img, must fail closed rather than crash or silently boot
        from garbage data."""
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw))
            (product_dir / "system.img").unlink()
            (product_dir / "super.img").write_bytes(b"\x00" * 4096)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertTrue(any("missing system image" in e for e in spec.errors))

    def test_extra_arg_without_allow_extra_args_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu", extra_args=["--foo"], allow_extra_args=False),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertTrue(any("--extra-arg requires --allow-extra-args" in e for e in spec.errors))

    def test_missing_netdev_backend_falls_back_to_no_network(self) -> None:
        """A qemu-system-x86_64 without libslirp must still boot (no -netdev user)."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            # The mock binary's "-netdev help" prints nothing (plain shell stub),
            # simulating a qemu build with no 'user' or 'passt' backend compiled in.
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)
            self.assertNotIn("-netdev", spec.argv)
            self.assertIn("-nic", spec.argv)
            self.assertEqual(spec.argv[spec.argv.index("-nic") + 1], "none")
            self.assertTrue(any("netdev backend" in w for w in spec.warnings))

    def test_netdev_user_used_when_available(self) -> None:
        """When the qemu binary reports 'user' via -netdev help, prefer it (with hostfwd)."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-netdev" ] && [ "$2" = "help" ]; then\n'
                '  echo "user"\n'
                '  echo "socket"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            qemu_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu", adb_port=5555),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertTrue(spec.bootable)
            self.assertIn("-netdev", spec.argv)
            netdev_value = spec.argv[spec.argv.index("-netdev") + 1]
            self.assertIn("user", netdev_value)
            self.assertIn("hostfwd=tcp::5555-:5555", netdev_value)

    def test_passt_listed_but_helper_missing_falls_back_to_no_network(self) -> None:
        """-netdev help listing 'passt' doesn't mean the passt(1) helper is installed."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-netdev" ] && [ "$2" = "help" ]; then\n'
                '  echo "passt"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            qemu_bin.chmod(0o755)
            with unittest.mock.patch("backends.qemu.shutil.which", return_value=None):
                spec = QemuBackend().plan(
                    product, LaunchOptions(backend="qemu"),
                    _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
                )
            self.assertTrue(spec.bootable)
            self.assertNotIn("-netdev", spec.argv)
            self.assertIn("-nic", spec.argv)
            self.assertTrue(any("netdev backend" in w for w in spec.warnings))

    def test_passt_used_when_helper_binary_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = Path(raw) / "qemu-system-x86_64"
            qemu_bin.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-netdev" ] && [ "$2" = "help" ]; then\n'
                '  echo "passt"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            qemu_bin.chmod(0o755)
            with unittest.mock.patch("backends.qemu.shutil.which", return_value="/usr/bin/passt"):
                spec = QemuBackend().plan(
                    product, LaunchOptions(backend="qemu"),
                    _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
                )
            self.assertTrue(spec.bootable)
            self.assertIn("-netdev", spec.argv)
            self.assertIn("passt", spec.argv[spec.argv.index("-netdev") + 1])
            self.assertTrue(any("passt" in w for w in spec.warnings))

    def test_firmware_dir_added_for_uninstalled_build(self) -> None:
        """A built-but-not-installed qemu (no ROMs at its compiled-in datadir)
        gets -L pointed at its own build-tree bundled firmware."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            build_dir = Path(raw) / "build"
            build_dir.mkdir()
            qemu_bin = build_dir / "qemu-system-x86_64"
            qemu_bin.write_text("#!/bin/sh\nexit 0\n")
            qemu_bin.chmod(0o755)
            bundle = build_dir / "qemu-bundle" / "usr" / "local" / "share" / "qemu"
            bundle.mkdir(parents=True)
            (bundle / "bios-256k.bin").write_bytes(b"\x00")
            with unittest.mock.patch(
                "backends.qemu._STANDARD_FIRMWARE_DIRS", (Path(raw) / "no-such-standard-dir",)
            ):
                spec = QemuBackend().plan(
                    product, LaunchOptions(backend="qemu"),
                    _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
                )
            self.assertTrue(spec.bootable)
            self.assertIn("-L", spec.argv)
            self.assertEqual(spec.argv[spec.argv.index("-L") + 1], str(bundle))
            self.assertTrue(any("ninja install" in w for w in spec.warnings))

    def test_firmware_dir_not_added_when_standard_install_has_it(self) -> None:
        """A properly 'ninja install'-ed / packaged qemu needs no -L override."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            standard = Path(raw) / "standard"
            standard.mkdir()
            (standard / "bios-256k.bin").write_bytes(b"\x00")
            with unittest.mock.patch("backends.qemu._STANDARD_FIRMWARE_DIRS", (standard,)):
                spec = QemuBackend().plan(
                    product, LaunchOptions(backend="qemu"),
                    _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
                )
            self.assertTrue(spec.bootable)
            self.assertNotIn("-L", spec.argv)


    def test_boot_img_native_parse_produces_bootable_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_boot_img_product(Path(raw))
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            # -kernel must point at a tempfile, not inside product_dir
            kernel_idx = spec.argv.index("-kernel") + 1
            self.assertNotIn(str(product_dir), spec.argv[kernel_idx])
            # -initrd must point at a tempfile, not inside product_dir
            initrd_idx = spec.argv.index("-initrd") + 1
            self.assertNotIn(str(product_dir), spec.argv[initrd_idx])
            # the raw vendor_boot / init_boot paths must NOT appear in argv
            if product.vendor_boot:
                self.assertNotIn(str(product.vendor_boot), " ".join(spec.argv))
            if product.init_boot:
                self.assertNotIn(str(product.init_boot), " ".join(spec.argv))

    def test_boot_img_native_parse_injects_virtio_modules_when_aosp_root_known(self) -> None:
        """When --aosp-root points at a tree with matching virtio modules,
        the combined initrd must contain them spliced into the generic
        ramdisk."""

        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            aosp_root = _make_aosp_virtio_module_tree(Path(raw) / "aosp", version)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu", aosp_root=aosp_root),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            initrd_path = Path(spec.argv[spec.argv.index("-initrd") + 1])
            initrd = initrd_path.read_bytes()
            self.assertIn(b"MOD_LEGACY", initrd)
            self.assertIn(b"MOD_PCI", initrd)
            self.assertIn(b"MOD_BLK", initrd)
            self.assertIn(b"MOD_GPU_DMA", initrd)
            self.assertIn(b"MOD_GPU", initrd)
            self.assertIn(b"lib/modules/5.10.160-android12/modules.load", initrd)

    def test_native_path_synthesizes_metadata_when_mke2fs_available(self) -> None:
        """When PRODUCT_OUT has no metadata.img and mke2fs is available,
        the QEMU backend synthesizes a 16MiB empty ext4 image, attaches it
        as the first drive, and injects the matching fstab line."""

        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            mke2fs_bin = Path(raw) / "mke2fs"
            mke2fs_bin.write_text(
                "#!/bin/sh\n"
                "# fake mke2fs: stamp the target file and exit successfully\n"
                'shift $(($# - 1))\n'
                'target="$1"\n'
                'printf "FAKEEXT4" | dd of="$target" bs=1 count=8 conv=notrunc 2>/dev/null\n'
                "exit 0\n"
            )
            mke2fs_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin, mke2fs=mke2fs_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            self.assertIn("metadata", spec.mapping.roles)
            metadata_path = Path(spec.mapping.roles["metadata"])
            self.assertNotIn(str(product_dir), str(metadata_path))
            self.assertEqual(metadata_path.stat().st_size, 16 * 1024 * 1024)
            # metadata must be the first drive and not marked read-only
            drive_values = [
                spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"
            ]
            self.assertTrue(drive_values)
            self.assertIn("metadata", drive_values[0])
            self.assertNotIn("readonly=on", drive_values[0])
            # the injected fstab must contain the metadata line
            initrd_path = Path(spec.argv[spec.argv.index("-initrd") + 1])
            initrd = initrd_path.read_bytes()
            self.assertIn(
                b"/dev/block/by-name/metadata /metadata ext4 "
                b"noatime,nosuid,nodev wait,first_stage_mount,formattable",
                initrd,
            )


class MiscPartitionSynthesisTests(unittest.TestCase):
    """Tests for synthetic /misc image needed by BootControl / Boot HAL."""

    def _make_qemu_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "qemu-system-x86_64"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_native_path_synthesizes_misc_image(self) -> None:
        """When PRODUCT_OUT has no misc.img, the QEMU backend synthesizes a
        1MiB empty raw image, attaches it writable, and injects the matching
        fstab line."""
        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            self.assertIn("misc", spec.mapping.roles)
            misc_path = Path(spec.mapping.roles["misc"])
            self.assertNotIn(str(product_dir), str(misc_path))
            self.assertEqual(misc_path.stat().st_size, 1 * 1024 * 1024)
            drive_values = [
                spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"
            ]
            misc_drives = [v for v in drive_values if "misc" in v]
            self.assertEqual(len(misc_drives), 1)
            self.assertNotIn("readonly=on", misc_drives[0])
            # the injected fstab must contain the misc line
            initrd_path = Path(spec.argv[spec.argv.index("-initrd") + 1])
            initrd = initrd_path.read_bytes()
            self.assertIn(
                b"/dev/block/by-name/misc /misc emmc defaults defaults",
                initrd,
            )
            self.assertTrue(
                any("synthesized empty /misc image" in w for w in spec.warnings)
            )

    def test_misc_synthesis_warns_when_image_cannot_be_created(self) -> None:
        """A failure to create the synthetic misc image must warn, not error."""
        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            bad_dir = Path(raw) / "bad"
            bad_dir.mkdir()
            bad_dir.chmod(0o555)
            call_count = 0
            real_mkdtemp = tempfile.mkdtemp

            def fake_mkdtemp() -> str:
                nonlocal call_count
                call_count += 1
                # The first mkdtemp call is for /misc; later calls are for the
                # native boot.img path's kernel/ramdisk tempfiles.
                if call_count == 1:
                    return str(bad_dir)
                return real_mkdtemp()

            with unittest.mock.patch("backends.qemu.tempfile.mkdtemp", side_effect=fake_mkdtemp):
                spec = QemuBackend().plan(
                    product,
                    LaunchOptions(backend="qemu"),
                    _caps(emulator=None, qemu=qemu_bin),
                    accel=_tcg(),
                )
            bad_dir.chmod(0o755)
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            self.assertNotIn("misc", spec.mapping.roles)
            self.assertTrue(
                any("could not synthesize /misc image" in w for w in spec.warnings)
            )

    def test_native_path_uses_existing_misc_image_when_present(self) -> None:
        """A real misc.img in optional_images must be used as-is, not replaced."""
        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            misc_img = product_dir / "misc.img"
            misc_img.write_bytes(b"REAL_MISC_IMAGE")
            product = inventory_product_out(product_dir)
            product.optional_images["misc"] = misc_img
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            self.assertIn("misc", spec.mapping.roles)
            self.assertEqual(Path(spec.mapping.roles["misc"]), misc_img)
            self.assertFalse(
                any("synthesized empty /misc image" in w for w in spec.warnings)
            )


class SlotSuffixDefaultTests(unittest.TestCase):
    """Tests for the default A/B slot suffix used when no super.img is present."""

    def _make_qemu_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "qemu-system-x86_64"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_native_path_emits_default_slot_suffix_a(self) -> None:
        """A flat PRODUCT_OUT without super.img must default to slot '_a' so
        BootControl sees a non-empty ro.boot.slot_suffix."""
        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            cmdline = spec.argv[spec.argv.index("-append") + 1]
            self.assertIn("androidboot.slot_suffix=_a", cmdline)

    def test_native_path_emits_nonzero_vsock_lights_port(self) -> None:
        """The native boot path must set androidboot.vsock_lights_port to a
        non-zero value so the Cuttlefish Lights HAL does not abort binding a
        vsock listener on port 0."""
        with tempfile.TemporaryDirectory() as raw:
            version = "5.10.160-android12"
            product_dir = _make_boot_img_product_with_kernel_version(Path(raw), version)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            cmdline = spec.argv[spec.argv.index("-append") + 1]
            match = re.search(r"androidboot\.vsock_lights_port=(\d+)", cmdline)
            self.assertIsNotNone(match, msg=cmdline)
            assert match is not None
            self.assertNotEqual(match.group(1), "0")


class AutoBackendTests(unittest.TestCase):
    def test_auto_ranchu_uses_emulator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_ranchu_product(Path(raw)))
            emulator_bin = Path(raw) / "emulator"
            emulator_bin.write_text("#!/bin/sh\n")
            plan = build_launch_plan(
                product,
                LaunchOptions(backend="auto", accel="tcg"),
                _caps(emulator=emulator_bin),
                accel=_tcg(),
                emulator_help=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertTrue(plan.spec.bootable)
            self.assertEqual(plan.spec.backend, "emulator")

    def test_auto_cuttlefish_recommends_launch_cvd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            plan = build_launch_plan(
                product,
                LaunchOptions(backend="auto", accel="tcg"),
                _caps(emulator=None),
                accel=_tcg(),
                emulator_help=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(plan.spec.bootable)
            self.assertEqual(plan.spec.argv, [])
            self.assertTrue(any("launch_cvd" in err for err in plan.spec.errors))

    def test_auto_cuttlefish_guidance_points_at_detected_launch_cvd(self) -> None:
        """When launch_cvd is actually found on this host and KVM works, say so."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            cvd_path = Path(raw) / "launch_cvd"
            plan = build_launch_plan(
                product,
                LaunchOptions(backend="auto", accel="tcg"),
                _caps(emulator=None, launch_cvd=cvd_path, kvm_accessible=True),
                accel=_tcg(),
                emulator_help=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(plan.spec.bootable)
            self.assertEqual(plan.spec.argv, [])
            joined = " ".join(plan.spec.errors)
            self.assertIn("launch_cvd", joined)
            self.assertIn(str(cvd_path), joined)

    def test_auto_cuttlefish_guidance_flags_missing_kvm(self) -> None:
        """launch_cvd is found but KVM isn't accessible — say how to fix it."""
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            cvd_path = Path(raw) / "launch_cvd"
            plan = build_launch_plan(
                product,
                LaunchOptions(backend="auto", accel="tcg"),
                _caps(emulator=None, launch_cvd=cvd_path, kvm_accessible=False),
                accel=_tcg(),
                emulator_help=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(plan.spec.bootable)
            joined = " ".join(plan.spec.errors)
            self.assertIn("launch_cvd", joined)
            self.assertIn("kvm", joined.lower())
            self.assertIn("usermod", joined)

    def test_auto_cuttlefish_guidance_when_launch_cvd_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            plan = build_launch_plan(
                product,
                LaunchOptions(backend="auto", accel="tcg"),
                _caps(emulator=None, launch_cvd=None),
                accel=_tcg(),
                emulator_help=EMULATOR_HELP,
                port_in_use=lambda _port: False,
            )
            self.assertFalse(plan.spec.bootable)
            joined = " ".join(plan.spec.errors)
            self.assertIn("launch_cvd", joined)
            self.assertIn("android-cuttlefish", joined)


class VendorPatchTests(unittest.TestCase):
    """Tests for wiring vendorpatch.patch_vendor_image into the QEMU backend."""

    def _make_qemu_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "qemu-system-x86_64"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def _plan(self, raw: str, patched_vendor: Path | None, patch_warning: str | None):
        product_dir = _make_boot_img_product_with_kernel_version(Path(raw), "5.10.160-android12")
        product = inventory_product_out(product_dir)
        qemu_bin = self._make_qemu_bin(raw)
        original_vendor = product.vendor
        patched = patched_vendor

        def fake_patch(
            vendor_image: Path,
            fstab_filename: str,
            misc_device: str,
            aosp_root: Path | None,
            product_out: Path | None,
            fsck_erofs: ToolInfo,
            mkfs_erofs: ToolInfo,
            build_image: ToolInfo,
        ) -> tuple[Path | None, str | None]:
            self.assertEqual(vendor_image, original_vendor)
            self.assertEqual(fstab_filename, "fstab.qemu")
            self.assertEqual(misc_device, "/dev/block/by-name/misc")
            return patched, patch_warning

        return fake_patch, QemuBackend().plan(
            product,
            LaunchOptions(backend="qemu"),
            _caps(
                emulator=None,
                qemu=qemu_bin,
                fsck_erofs=Path(raw) / "fsck.erofs",
                mkfs_erofs=Path(raw) / "mkfs.erofs",
                build_image=Path(raw) / "build_image",
            ),
            accel=_tcg(),
        )

    def test_patched_vendor_substituted_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            envsetup = Path(raw) / "build" / "envsetup.sh"
            envsetup.parent.mkdir(parents=True)
            envsetup.write_text("")
            patched_vendor = Path(raw) / "patched_vendor.img"
            patched_vendor.write_bytes(b"PATCHED")
            fake_patch, spec = self._plan(raw, patched_vendor, None)
            with unittest.mock.patch("backends.qemu.patch_vendor_image", side_effect=fake_patch):
                spec = self._plan(raw, patched_vendor, None)[1]
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            vendor_drives = [v for v in drive_values if "patched_vendor.img" in v]
            self.assertEqual(len(vendor_drives), 1)
            self.assertFalse(
                any("vendor.img will not be patched" in w for w in spec.warnings)
            )

    def test_original_vendor_used_when_patch_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            envsetup = Path(raw) / "build" / "envsetup.sh"
            envsetup.parent.mkdir(parents=True)
            envsetup.write_text("")
            fake_patch, spec = self._plan(raw, None, "patch unavailable")
            with unittest.mock.patch("backends.qemu.patch_vendor_image", side_effect=fake_patch):
                spec = self._plan(raw, None, "patch unavailable")[1]
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            vendor_drives = [v for v in drive_values if "vendor.img" in v]
            self.assertEqual(len(vendor_drives), 1)
            self.assertNotIn("patched_vendor.img", vendor_drives[0])
            self.assertTrue(any("patch unavailable" in w for w in spec.warnings))


class SparseImageConversionTests(unittest.TestCase):
    """Tests for converting Android sparse partition images before QEMU drive attachment."""

    def _make_qemu_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "qemu-system-x86_64"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def _make_simg2img_bin(self, tmpdir: str) -> Path:
        p = Path(tmpdir) / "simg2img"
        p.write_text(
            "#!/bin/sh\n"
            "# fake simg2img: copy input to output\n"
            'cp "$1" "$2"\n'
            "exit 0\n"
        )
        p.chmod(0o755)
        return p

    def test_sparse_userdata_is_converted_before_drive_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            # Make system non-sparse so only userdata needs conversion.
            (product_dir / "system.img").write_bytes(b"\x00" * 4096)
            userdata_path = product_dir / "userdata.img"
            write_sparse(userdata_path)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            simg2img_bin = self._make_simg2img_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin, simg2img=simg2img_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertNotIn(str(userdata_path), data_drives[0])
            self.assertTrue(
                any("converted Android sparse image" in w and "userdata" in w for w in spec.warnings)
            )

    def test_non_sparse_userdata_attached_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            # Make system non-sparse too; userdata is already raw.
            (product_dir / "system.img").write_bytes(b"\x00" * 4096)
            userdata_path = product_dir / "userdata.img"
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            # Provide a fake simg2img that would fail if called; the non-sparse
            # path must not trigger conversion at all.
            simg2img_bin = Path(raw) / "simg2img"
            simg2img_bin.write_text("#!/bin/sh\nexit 1\n")
            simg2img_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin, simg2img=simg2img_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertIn(str(userdata_path), data_drives[0])
            self.assertFalse(
                any("converted Android sparse image" in w for w in spec.warnings)
            )

    def test_sparse_userdata_fallback_when_simg2img_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            (product_dir / "system.img").write_bytes(b"\x00" * 4096)
            userdata_path = product_dir / "userdata.img"
            write_sparse(userdata_path)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertIn(str(userdata_path), data_drives[0])
            self.assertTrue(
                any("simg2img is not available" in w and "userdata" in w for w in spec.warnings)
            )

    def test_sparse_userdata_fallback_when_simg2img_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product_dir = make_generic_x86_64_product(Path(raw), with_userdata=True)
            (product_dir / "system.img").write_bytes(b"\x00" * 4096)
            userdata_path = product_dir / "userdata.img"
            write_sparse(userdata_path)
            product = inventory_product_out(product_dir)
            qemu_bin = self._make_qemu_bin(raw)
            simg2img_bin = Path(raw) / "simg2img"
            simg2img_bin.write_text("#!/bin/sh\nexit 1\n")
            simg2img_bin.chmod(0o755)
            spec = QemuBackend().plan(
                product,
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin, simg2img=simg2img_bin),
                accel=_tcg(),
            )
            self.assertTrue(spec.bootable, msg=f"errors={spec.errors}")
            drive_values = [spec.argv[i + 1] for i, a in enumerate(spec.argv) if a == "-drive"]
            data_drives = [v for v in drive_values if "userdata" in v]
            self.assertEqual(len(data_drives), 1)
            self.assertIn(str(userdata_path), data_drives[0])
            self.assertTrue(
                any("could not convert Android sparse image" in w and "userdata" in w for w in spec.warnings)
            )


if __name__ == "__main__":
    unittest.main()
