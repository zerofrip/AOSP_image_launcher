"""Tests for src/vendorpatch.py. Subprocess calls are mocked."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

import support  # noqa: F401

from capabilities import ToolInfo
from vendorpatch import EROFS_MAGIC, EROFS_MAGIC_OFFSET, patch_vendor_image


def _tool(path: Path | None, name: str) -> ToolInfo:
    return ToolInfo(name=name, path=path, available=path is not None)


def _make_erofs_image(path: Path) -> None:
    data = bytearray(EROFS_MAGIC_OFFSET + len(EROFS_MAGIC) + 4)
    data[EROFS_MAGIC_OFFSET:EROFS_MAGIC_OFFSET + len(EROFS_MAGIC)] = EROFS_MAGIC
    path.write_bytes(bytes(data))


class ErofsMagicTests(unittest.TestCase):
    def test_valid_magic_recognized(self) -> None:
        from vendorpatch import _is_erofs_image

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            _make_erofs_image(path)
            self.assertTrue(_is_erofs_image(path))

    def test_invalid_magic_rejected(self) -> None:
        from vendorpatch import _is_erofs_image

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            path.write_bytes(b"\x00" * 2048)
            self.assertFalse(_is_erofs_image(path))

    def test_short_file_rejected(self) -> None:
        from vendorpatch import _is_erofs_image

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            path.write_bytes(b"\x00" * 64)
            self.assertFalse(_is_erofs_image(path))


class ToolAvailabilityGateTests(unittest.TestCase):
    def test_missing_fsck_erofs_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            _make_erofs_image(path)
            patched, warning = patch_vendor_image(
                path,
                "fstab.qemu",
                "/dev/block/by-name/misc",
                Path("/aosp"),
                Path("/aosp/out/target/product/foo"),
                fsck_erofs=_tool(None, "fsck.erofs"),
                mkfs_erofs=_tool(Path("/bin/mkfs.erofs"), "mkfs.erofs"),
                build_image=_tool(Path("/bin/build_image"), "build_image"),
            )
        self.assertIsNone(patched)
        self.assertIsNotNone(warning)
        self.assertIn("fsck.erofs", warning)

    def test_missing_mkfs_erofs_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            _make_erofs_image(path)
            patched, warning = patch_vendor_image(
                path,
                "fstab.qemu",
                "/dev/block/by-name/misc",
                Path("/aosp"),
                Path("/aosp/out/target/product/foo"),
                fsck_erofs=_tool(Path("/bin/fsck.erofs"), "fsck.erofs"),
                mkfs_erofs=_tool(None, "mkfs.erofs"),
                build_image=_tool(Path("/bin/build_image"), "build_image"),
            )
        self.assertIsNone(patched)
        self.assertIsNotNone(warning)
        self.assertIn("mkfs.erofs", warning)

    def test_missing_build_image_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "vendor.img"
            _make_erofs_image(path)
            patched, warning = patch_vendor_image(
                path,
                "fstab.qemu",
                "/dev/block/by-name/misc",
                Path("/aosp"),
                Path("/aosp/out/target/product/foo"),
                fsck_erofs=_tool(Path("/bin/fsck.erofs"), "fsck.erofs"),
                mkfs_erofs=_tool(Path("/bin/mkfs.erofs"), "mkfs.erofs"),
                build_image=_tool(None, "build_image"),
            )
        self.assertIsNone(patched)
        self.assertIsNotNone(warning)
        self.assertIn("build_image", warning)


class PropertiesFileTests(unittest.TestCase):
    def test_real_vendor_prop_used_with_avb_lines_stripped(self) -> None:
        from vendorpatch import _build_properties_file

        with tempfile.TemporaryDirectory() as raw:
            aosp_root = Path(raw) / "aosp"
            prop_dir = (
                aosp_root
                / "out/soong/.intermediates/build/soong/fsgen"
                / "foo_generated_vendor_image/android_common"
            )
            prop_dir.mkdir(parents=True)
            prop_dir.joinpath("prop").write_text(
                "fs_type=erofs\n"
                "mount_point=vendor\n"
                "partition_name=vendor\n"
                "avb_include_desc=foo\n"
                "selinux_fc=/path/to/file_contexts.bin\n",
                encoding="utf-8",
            )
            tmpdir = Path(raw) / "work"
            tmpdir.mkdir()
            prop_path = _build_properties_file(
                aosp_root, Path("/product/out"), tmpdir
            )
            text = prop_path.read_text(encoding="utf-8")
            self.assertIn("fs_type=erofs", text)
            self.assertIn("selinux_fc=/path/to/file_contexts.bin", text)
            self.assertNotIn("avb_include_desc", text)
            self.assertNotIn("avb_", text)

    def test_zero_matches_uses_minimal_fallback(self) -> None:
        from vendorpatch import _build_properties_file, _FILE_CONTEXTS_BIN

        with tempfile.TemporaryDirectory() as raw:
            aosp_root = Path(raw) / "aosp"
            tmpdir = Path(raw) / "work"
            tmpdir.mkdir()
            prop_path = _build_properties_file(
                aosp_root, Path("/product/out"), tmpdir
            )
            text = prop_path.read_text(encoding="utf-8")
            self.assertIn("fs_type=erofs", text)
            self.assertIn("mount_point=vendor", text)
            self.assertIn("partition_name=vendor", text)
            self.assertIn("disable_sparse=true", text)
            self.assertIn(str(Path(_FILE_CONTEXTS_BIN)), text)

    def test_multiple_matches_uses_minimal_fallback(self) -> None:
        from vendorpatch import _build_properties_file

        with tempfile.TemporaryDirectory() as raw:
            aosp_root = Path(raw) / "aosp"
            base = (
                aosp_root
                / "out/soong/.intermediates/build/soong/fsgen"
            )
            for name in ("foo", "bar"):
                d = base / f"{name}_generated_vendor_image/android_common"
                d.mkdir(parents=True)
                d.joinpath("prop").write_text("fs_type=erofs\n", encoding="utf-8")
            tmpdir = Path(raw) / "work"
            tmpdir.mkdir()
            prop_path = _build_properties_file(
                aosp_root, Path("/product/out"), tmpdir
            )
            text = prop_path.read_text(encoding="utf-8")
            self.assertIn("fs_type=erofs", text)
            self.assertIn("disable_sparse=true", text)


class PatchVendorImageWorkflowTests(unittest.TestCase):
    def _fake_run(self, argv, **_kwargs):
        import subprocess

        op = Path(argv[0]).name
        if op == "fsck.erofs":
            # argv: [fsck.erofs, --extract=<dir>, vendor.img]
            extract_arg = argv[1]
            extract_dir = Path(extract_arg.split("=", 1)[1])
            extract_dir.mkdir(parents=True, exist_ok=True)
            (extract_dir / "existing.txt").write_text("hello", encoding="utf-8")
        elif op == "build_image":
            # argv: [build_image, extract_dir, prop_file, output, product_out]
            output = Path(argv[3])
            output.write_bytes(b"PATCHED_VENDOR")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_successful_patch_injects_fstab_and_returns_temp_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vendor = Path(raw) / "vendor.img"
            _make_erofs_image(vendor)
            aosp_root = Path(raw) / "aosp"
            product_out = Path(raw) / "product"
            fsck = Path(raw) / "fsck.erofs"
            mkfs = Path(raw) / "mkfs.erofs"
            build = Path(raw) / "build_image"
            for p in (fsck, mkfs, build):
                p.write_text("#!/bin/sh\n")
                p.chmod(0o755)

            with unittest.mock.patch("vendorpatch.subprocess.run", side_effect=self._fake_run):
                patched, warning = patch_vendor_image(
                    vendor,
                    "fstab.cutf_cvm",
                    "/dev/block/by-name/misc",
                    aosp_root,
                    product_out,
                    fsck_erofs=_tool(fsck, "fsck.erofs"),
                    mkfs_erofs=_tool(mkfs, "mkfs.erofs"),
                    build_image=_tool(build, "build_image"),
                )

            self.assertIsNotNone(patched)
            self.assertIsNone(warning)
            self.assertEqual(patched.read_bytes(), b"PATCHED_VENDOR")
            self.assertNotEqual(patched, vendor)

    def test_fsck_erofs_failure_returns_warning(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as raw:
            vendor = Path(raw) / "vendor.img"
            _make_erofs_image(vendor)
            fsck = Path(raw) / "fsck.erofs"
            mkfs = Path(raw) / "mkfs.erofs"
            build = Path(raw) / "build_image"

            def failing_run(argv, **_kwargs):
                return subprocess.CompletedProcess(argv, 1, "", "extract failed")

            with unittest.mock.patch("vendorpatch.subprocess.run", side_effect=failing_run):
                patched, warning = patch_vendor_image(
                    vendor,
                    "fstab.qemu",
                    "/dev/block/by-name/misc",
                    Path(raw) / "aosp",
                    Path(raw) / "product",
                    fsck_erofs=_tool(fsck, "fsck.erofs"),
                    mkfs_erofs=_tool(mkfs, "mkfs.erofs"),
                    build_image=_tool(build, "build_image"),
                )
            self.assertIsNone(patched)
            self.assertIsNotNone(warning)
            self.assertIn("fsck.erofs", warning)
            self.assertIn("extract failed", warning)

    def test_build_image_failure_returns_warning(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as raw:
            vendor = Path(raw) / "vendor.img"
            _make_erofs_image(vendor)
            fsck = Path(raw) / "fsck.erofs"
            mkfs = Path(raw) / "mkfs.erofs"
            build = Path(raw) / "build_image"

            def fake_run(argv, **_kwargs):
                op = Path(argv[0]).name
                if op == "fsck.erofs":
                    extract_arg = argv[1]
                    extract_dir = Path(extract_arg.split("=", 1)[1])
                    extract_dir.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(argv, 0 if op != "build_image" else 2, "", "build failed")

            with unittest.mock.patch("vendorpatch.subprocess.run", side_effect=fake_run):
                patched, warning = patch_vendor_image(
                    vendor,
                    "fstab.qemu",
                    "/dev/block/by-name/misc",
                    Path(raw) / "aosp",
                    Path(raw) / "product",
                    fsck_erofs=_tool(fsck, "fsck.erofs"),
                    mkfs_erofs=_tool(mkfs, "mkfs.erofs"),
                    build_image=_tool(build, "build_image"),
                )
            self.assertIsNone(patched)
            self.assertIsNotNone(warning)
            self.assertIn("build_image", warning)
            self.assertIn("build failed", warning)


if __name__ == "__main__":
    unittest.main()
