"""Accelerator selection tests. Mocks only — no real qemu/emulator required."""

from __future__ import annotations

import unittest
from pathlib import Path

import support  # noqa: F401  — inserts src/ on sys.path

from capabilities import (
    AccelProbe,
    HostEnvironment,
    parse_accel_help,
    probe_accelerators,
    select_accelerator,
)
from errors import AcceleratorError


def _host(
    *,
    platform: str,
    kvm_accessible: bool = False,
    kvm_exists: bool = False,
) -> HostEnvironment:
    return HostEnvironment(
        platform=platform,
        is_windows=platform == "windows",
        is_linux=platform in {"linux", "wsl"},
        is_wsl=platform == "wsl",
        kvm_node_exists=kvm_exists,
        kvm_accessible=kvm_accessible,
        cpu_count=4,
    )


class AcceleratorSelectionTests(unittest.TestCase):
    def test_emulator_accel_off_is_reported_as_tcg(self) -> None:
        def runner(_argv):
            from capabilities import ProbeResult

            return ProbeResult(
                argv=tuple(_argv),
                returncode=0,
                stdout=(
                    "Valid values for <mode> are:\n"
                    "  auto\n"
                    "  off  Disables acceleration entirely.\n"
                    "  on\n"
                ),
                stderr="",
            )

        probe = probe_accelerators(Path("/mock/emulator"), "emulator", runner=runner)
        self.assertIn("tcg", probe.reported)

    def test_windows_whpx(self) -> None:
        host = _host(platform="windows")
        probe = AccelProbe(
            executable=Path("C:/Android/emulator.exe"),
            kind="emulator",
            reported=("whpx", "tcg"),
            raw_output="WHPX\nTCG\n",
        )
        selected = select_accelerator(
            "whpx", host, probe, executable_format_name="pe"
        )
        self.assertEqual(selected.selected, "whpx")
        self.assertFalse(selected.fallback)
        auto = select_accelerator("auto", host, probe, executable_format_name="pe")
        self.assertEqual(auto.selected, "whpx")

    def test_wsl_rejects_whpx(self) -> None:
        host = _host(platform="wsl", kvm_accessible=True, kvm_exists=True)
        probe = AccelProbe(
            executable=Path("/usr/bin/qemu-system-x86_64"),
            kind="qemu",
            reported=("kvm", "tcg", "whpx"),
            raw_output="kvm\ntcg\nwhpx\n",
        )
        with self.assertRaises(AcceleratorError) as ctx:
            select_accelerator("whpx", host, probe, executable_format_name="elf")
        message = str(ctx.exception).lower()
        self.assertTrue("whpx" in message and ("wsl" in message or "elf" in message))

    def test_linux_elf_rejects_whpx(self) -> None:
        host = _host(platform="linux", kvm_accessible=True, kvm_exists=True)
        probe = AccelProbe(
            executable=Path("/usr/bin/qemu-system-x86_64"),
            kind="qemu",
            reported=("kvm", "tcg", "whpx"),
            raw_output="kvm\ntcg\nwhpx\n",
        )
        with self.assertRaises(AcceleratorError):
            select_accelerator("whpx", host, probe, executable_format_name="elf")

    def test_linux_kvm(self) -> None:
        host = _host(platform="linux", kvm_accessible=True, kvm_exists=True)
        probe = AccelProbe(
            executable=Path("/usr/bin/qemu-system-x86_64"),
            kind="qemu",
            reported=("kvm", "tcg"),
            raw_output="kvm\ntcg\n",
        )
        kvm = select_accelerator("kvm", host, probe, executable_format_name="elf")
        self.assertEqual(kvm.selected, "kvm")
        self.assertFalse(kvm.fallback)
        auto = select_accelerator("auto", host, probe, executable_format_name="elf")
        self.assertEqual(auto.selected, "kvm")
        self.assertFalse(auto.fallback)

    def test_auto_falls_back_to_tcg_with_reason(self) -> None:
        host = _host(platform="linux", kvm_accessible=False, kvm_exists=False)
        probe = AccelProbe(
            executable=Path("/usr/bin/qemu-system-x86_64"),
            kind="qemu",
            reported=("tcg",),
            raw_output="tcg\n",
            error="accel probe: kvm not usable",
        )
        selected = select_accelerator("auto", host, probe, executable_format_name="elf")
        self.assertEqual(selected.selected, "tcg")
        self.assertTrue(selected.fallback)
        self.assertTrue(selected.reason)
        self.assertIn("tcg", selected.reason.lower())

    def test_explicit_whpx_unavailable_fails(self) -> None:
        host = _host(platform="windows")
        probe = AccelProbe(
            executable=Path("C:/qemu/qemu-system-x86_64.exe"),
            kind="qemu",
            reported=("tcg",),
            raw_output="tcg\n",
        )
        with self.assertRaises(AcceleratorError) as ctx:
            select_accelerator("whpx", host, probe, executable_format_name="pe")
        self.assertIn("whpx", str(ctx.exception).lower())

    def test_explicit_kvm_unavailable_fails(self) -> None:
        host = _host(platform="linux", kvm_accessible=False, kvm_exists=False)
        probe = AccelProbe(
            executable=Path("/usr/bin/qemu-system-x86_64"),
            kind="qemu",
            reported=("tcg",),
            raw_output="tcg\n",
        )
        with self.assertRaises(AcceleratorError):
            select_accelerator("kvm", host, probe, executable_format_name="elf")

    def test_parse_emulator_check_negatives(self) -> None:
        text = (
            "accel:\n"
            "1\n"
            "WHPX (version 0) is not installed/usable.\n"
            "KVM (version 12) is installed and usable.\n"
        )
        reported = parse_accel_help(text)
        self.assertIn("kvm", reported)
        self.assertNotIn("whpx", reported)


if __name__ == "__main__":
    unittest.main()
