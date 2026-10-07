"""PRODUCT_OUT resolution and inventory tests using synthetic fixtures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import (
    make_aosp_tree,
    make_arm64_product,
    make_cuttlefish_product,
    make_incomplete_ranchu,
    make_ranchu_product,
    write_bzimage,
    write_bytes,
    write_elf,
)

from errors import AmbiguousProductOutError, ArchitectureError, ProductOutError
from product_out import (
    KERNEL_CANDIDATES,
    RAMDISK_CANDIDATES,
    inventory_product_out,
    resolve_product_out,
)


class ProductOutResolveTests(unittest.TestCase):
    def test_explicit_product_out_precedes_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ranchu = make_ranchu_product(root / "a", "emu64x")
            cuttlefish = make_cuttlefish_product(root / "b", "vsoc_x86_64")
            resolved = resolve_product_out(
                str(ranchu),
                environ={"PRODUCT_OUT": str(cuttlefish)},
                cwd=root,
                is_wsl=False,
            )
            self.assertEqual(resolved.path, ranchu.resolve())
            self.assertEqual(resolved.source, "explicit")

    def test_product_out_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ranchu = make_ranchu_product(root, "emu64x")
            resolved = resolve_product_out(
                None,
                environ={"PRODUCT_OUT": str(ranchu)},
                cwd=root,
                is_wsl=False,
            )
            self.assertEqual(resolved.path, ranchu.resolve())
            self.assertEqual(resolved.source, "env:PRODUCT_OUT")

    def test_relative_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tree = make_aosp_tree(Path(raw), ["emu64x"])
            relative = resolve_product_out(
                "out/target/product/emu64x",
                environ={},
                cwd=tree,
                is_wsl=False,
            )
            self.assertEqual(relative.path, (tree / "out" / "target" / "product" / "emu64x").resolve())
            absolute = resolve_product_out(
                str((tree / "out" / "target" / "product" / "emu64x").resolve()),
                environ={},
                cwd=tree,
                is_wsl=False,
            )
            self.assertEqual(absolute.path, relative.path)

    def test_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "does-not-exist"
            with self.assertRaises(ProductOutError) as ctx:
                resolve_product_out(str(missing), environ={}, cwd=Path(raw), is_wsl=False)
            self.assertIn("does not exist", str(ctx.exception).lower())

    def test_multiple_targets_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tree = make_aosp_tree(Path(raw), ["emu64x", "vsoc_x86_64"])
            with self.assertRaises(AmbiguousProductOutError) as ctx:
                resolve_product_out(str(tree), environ={}, cwd=tree, is_wsl=False)
            names = ctx.exception.candidates
            self.assertGreaterEqual(len(names), 2)
            joined = "\n".join(names)
            self.assertIn("emu64x", joined)
            self.assertIn("vsoc_x86_64", joined)
            self.assertIn("emu64x", str(ctx.exception))
            self.assertIn("vsoc_x86_64", str(ctx.exception))

    def test_target_selects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tree = make_aosp_tree(Path(raw), ["emu64x", "vsoc_x86_64"])
            resolved = resolve_product_out(
                str(tree),
                target="emu64x",
                environ={},
                cwd=tree,
                is_wsl=False,
            )
            self.assertEqual(resolved.path.name, "emu64x")

    def test_spaces_and_unicode_in_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = Path(raw) / "prod uct dir"
            product = make_ranchu_product(parent, "emu64x_テスト")
            resolved = resolve_product_out(
                str(product),
                environ={},
                cwd=Path(raw),
                is_wsl=False,
            )
            artifacts = inventory_product_out(resolved)
            self.assertEqual(artifacts.architecture, "x86_64")
            self.assertEqual(artifacts.family, "emulator_ranchu")
            self.assertTrue(artifacts.product_out.exists())


class ProductOutInventoryTests(unittest.TestCase):
    def test_x86_64_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            artifacts = inventory_product_out(product)
            self.assertEqual(artifacts.architecture, "x86_64")

    def test_arm64_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_arm64_product(Path(raw), "generic_arm64")
            with self.assertRaises(ArchitectureError) as ctx:
                inventory_product_out(product)
            self.assertIn("not x86_64", str(ctx.exception))

    def test_ranchu_classified_emulator_ranchu(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            artifacts = inventory_product_out(product)
            self.assertEqual(artifacts.family, "emulator_ranchu")
            self.assertFalse(artifacts.incomplete)
            self.assertIsNotNone(artifacts.kernel)
            self.assertIsNotNone(artifacts.ramdisk)
            self.assertIsNotNone(artifacts.system)

    def test_cuttlefish_classified_and_unsupported_images(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_cuttlefish_product(Path(raw), "vsoc_x86_64")
            artifacts = inventory_product_out(product)
            self.assertEqual(artifacts.family, "cuttlefish")
            self.assertTrue(artifacts.dynamic_partitions)
            vendor_boot = artifacts.artifact("vendor_boot")
            super_img = artifacts.artifact("super")
            self.assertIsNotNone(vendor_boot)
            self.assertIsNotNone(super_img)
            assert vendor_boot is not None
            assert super_img is not None
            self.assertEqual(vendor_boot.status, "unsupported")
            self.assertEqual(super_img.status, "unsupported")
            self.assertIsNotNone(artifacts.vendor_boot)
            self.assertIsNotNone(artifacts.super_image)

    def test_kernel_candidate_priority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            write_elf(product / "kernel-ranchu", "x86_64")
            write_elf(product / "kernel", "x86_64")
            write_bzimage(product / "bzImage")
            write_elf(product / "Image", "x86_64")
            artifacts = inventory_product_out(product)
            self.assertIsNotNone(artifacts.kernel)
            assert artifacts.kernel is not None
            self.assertEqual(artifacts.kernel.name, KERNEL_CANDIDATES[0])
            self.assertEqual(artifacts.kernel.name, "kernel-ranchu-64")

    def test_ramdisk_candidate_priority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            write_bytes(product / "ramdisk.img", b"\x1f\x8b" + b"\x00" * 4)
            write_bytes(product / "initramfs.img", b"070701")
            artifacts = inventory_product_out(product)
            self.assertIsNotNone(artifacts.ramdisk)
            assert artifacts.ramdisk is not None
            self.assertEqual(artifacts.ramdisk.name, RAMDISK_CANDIDATES[0])
            self.assertEqual(artifacts.ramdisk.name, "ramdisk-qemu.img")

    def test_incomplete_ranchu(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_incomplete_ranchu(Path(raw), "emu64x")
            artifacts = inventory_product_out(product)
            self.assertEqual(artifacts.family, "emulator_ranchu")
            self.assertTrue(artifacts.incomplete)
            codes = {issue.code for issue in artifacts.issues}
            self.assertIn("incomplete_ranchu", codes)
            self.assertIn("missing_ramdisk", codes)
            self.assertIn("missing_system", codes)
            self.assertIsNone(artifacts.ramdisk)
            self.assertIsNone(artifacts.system)


if __name__ == "__main__":
    unittest.main()
