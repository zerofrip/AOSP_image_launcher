"""Backend planning tests. Emulator -help is mocked; no real qemu spawn."""

from __future__ import annotations

import dataclasses
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
    host = _host_linux() if kvm_accessible else dataclasses.replace(_host_linux(), kvm_accessible=False)
    return Capabilities(
        host=host,
        emulator=emu,
        qemu=qemu_tool,
        emulator_check=_missing_tool("emulator-check"),
        unpack_bootimg=_missing_tool("unpack_bootimg"),
        lpunpack=_missing_tool("lpunpack"),
        simg2img=_missing_tool("simg2img"),
        avbtool=_missing_tool("avbtool"),
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
    def test_fail_closed_cuttlefish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_cuttlefish_product(Path(raw)))
            spec = QemuBackend().plan(
                product, LaunchOptions(backend="qemu"), _caps(emulator=None), accel=_tcg()
            )
            self.assertEqual(spec.argv, [])
            self.assertFalse(spec.bootable)
            joined = " ".join(spec.errors).lower()
            self.assertIn("launch_cvd", joined)
            self.assertNotIn("-initrd", spec.argv)
            self.assertTrue(any("vendor_boot" in err for err in spec.errors))
            self.assertTrue(any("super.img" in err.lower() or "dynamic" in err.lower() for err in spec.errors))

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


def _make_aosp_virtio_module_tree(root: Path, version: str) -> Path:
    """Create a fake AOSP ``system_dlkm_staging`` tree with the 4 virtio modules."""

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

    def test_super_img_on_cuttlefish_still_refused(self) -> None:
        """cuttlefish/emulator_ranchu must keep being refused outright when
        an actual super.img is present; only the unknown family attempts
        native liblp extraction."""
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
            self.assertIn(b"lib/modules/5.10.160-android12/modules.load", initrd)


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


if __name__ == "__main__":
    unittest.main()
