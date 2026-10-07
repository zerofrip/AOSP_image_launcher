"""Backend planning tests. Emulator -help is mocked; no real qemu spawn."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import support  # noqa: F401
from support import (
    make_cuttlefish_product,
    make_generic_x86_64_product,
    make_incomplete_ranchu,
    make_ranchu_product,
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


def _caps(*, emulator: Path | None, qemu: Path | None = None) -> Capabilities:
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
    return Capabilities(
        host=_host_linux(),
        emulator=emu,
        qemu=qemu_tool,
        emulator_check=_missing_tool("emulator-check"),
        unpack_bootimg=_missing_tool("unpack_bootimg"),
        lpunpack=_missing_tool("lpunpack"),
        simg2img=_missing_tool("simg2img"),
        avbtool=_missing_tool("avbtool"),
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

    def test_super_img_on_generic_family_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = inventory_product_out(make_generic_x86_64_product(Path(raw)))
            qemu_bin = self._make_qemu_bin(raw)
            spec = QemuBackend().plan(
                dataclasses.replace(product, dynamic_partitions=True),
                LaunchOptions(backend="qemu"),
                _caps(emulator=None, qemu=qemu_bin), accel=_tcg(),
            )
            self.assertFalse(spec.bootable)
            self.assertEqual(spec.argv, [])

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


if __name__ == "__main__":
    unittest.main()
