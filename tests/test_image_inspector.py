"""Image header classification: magic bytes only, never bootable images."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import (
    SPARSE_MAGIC,
    write_boot_img,
    write_bzimage,
    write_elf,
    write_sparse,
    write_sparse_super,
    write_vendor_boot,
)

from image_inspector import classify_bytes, inspect_file


class ImageInspectorTests(unittest.TestCase):
    def test_sparse_magic_little_endian(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_sparse(Path(raw) / "system.img")
            inspection = inspect_file(path)
            self.assertEqual(inspection.format, "android_sparse")
            self.assertTrue(any("3A FF 26 ED" in note for note in inspection.notes))
            self.assertTrue(any(f"0x{SPARSE_MAGIC:08X}" in note for note in inspection.notes))
            fmt, _arch, notes = classify_bytes(path.read_bytes()[:28], path.stat().st_size)
            self.assertEqual(fmt, "android_sparse")
            self.assertTrue(any("3A FF 26 ED" in note for note in notes))

    def test_android_boot_magic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_boot_img(Path(raw) / "boot.img")
            inspection = inspect_file(path)
            self.assertEqual(inspection.format, "boot_android")

    def test_vendor_boot_magic(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_vendor_boot(Path(raw) / "vendor_boot.img")
            inspection = inspect_file(path)
            self.assertEqual(inspection.format, "vendor_boot")

    def test_elf_x86_64_vs_aarch64(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            x86 = write_elf(Path(raw) / "kernel-x86_64", "x86_64")
            arm = write_elf(Path(raw) / "kernel-arm64", "aarch64")
            x86_ins = inspect_file(x86)
            arm_ins = inspect_file(arm)
            self.assertEqual(x86_ins.format, "elf")
            self.assertEqual(x86_ins.architecture, "x86_64")
            self.assertEqual(arm_ins.format, "elf")
            self.assertEqual(arm_ins.architecture, "arm64")
            self.assertNotEqual(x86_ins.architecture, arm_ins.architecture)

    def test_bzimage_hdrs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_bzimage(Path(raw) / "bzImage")
            inspection = inspect_file(path)
            self.assertEqual(inspection.format, "bzimage")
            self.assertEqual(inspection.architecture, "x86_64")
            self.assertTrue(any("HdrS" in note for note in inspection.notes))

    def test_super_sparse_not_raw_partition(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_sparse_super(Path(raw) / "super.img")
            inspection = inspect_file(path)
            self.assertEqual(inspection.format, "android_sparse_super")
            self.assertNotEqual(inspection.format, "raw_or_unknown")
            self.assertNotIn(inspection.format, {"unknown", "raw_or_unknown"})
            self.assertTrue(any("gDla" in note for note in inspection.notes))


if __name__ == "__main__":
    unittest.main()
