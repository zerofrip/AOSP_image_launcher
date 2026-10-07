"""Instance userdata tests. All paths are under tempfile — never PRODUCT_OUT."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from support import make_ranchu_product

from errors import UserdataError
from product_out import inventory_product_out
from userdata import default_state_dir, prepare_instance, reset_instance_userdata


class StateDirTests(unittest.TestCase):
    def test_linux_xdg_not_dot_startloader(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            chosen = default_state_dir(
                environ={"XDG_STATE_HOME": raw, "HOME": raw},
                is_windows=False,
            )
            self.assertEqual(chosen, Path(raw) / "startloader")
            self.assertNotEqual(chosen, Path(raw) / ".startloader")

    def test_linux_home_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            chosen = default_state_dir(
                environ={"HOME": raw},
                is_windows=False,
            )
            self.assertEqual(chosen, Path(raw) / ".local" / "state" / "startloader")
            self.assertNotEqual(chosen, Path(raw) / ".startloader")
            override = default_state_dir(
                environ={"STARTLOADER_STATE_DIR": str(Path(raw) / "custom")},
                is_windows=False,
            )
            self.assertEqual(override, Path(raw) / "custom")

    def test_windows_localappdata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            chosen = default_state_dir(
                environ={"LOCALAPPDATA": raw},
                is_windows=True,
            )
            self.assertEqual(chosen, Path(raw) / "startloader")

    def test_work_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            chosen = default_state_dir(environ={}, work_dir=raw, is_windows=False)
            self.assertEqual(chosen, Path(raw))

    def test_work_dir_rejects_dotdot(self) -> None:
        with self.assertRaises(UserdataError):
            default_state_dir(environ={}, work_dir="../escape", is_windows=False)


class UserdataCopyTests(unittest.TestCase):
    def test_copy_to_instance_leaves_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            source = product.userdata
            assert source is not None
            original = source.read_bytes()
            source_stat = source.stat()
            work = root / "state"
            instance = prepare_instance(product, work_dir=work, environ={})
            self.assertIsNotNone(instance.userdata)
            assert instance.userdata is not None
            self.assertTrue(instance.userdata.is_file())
            self.assertEqual(instance.userdata.read_bytes(), original)
            self.assertEqual(source.stat().st_mtime_ns, source_stat.st_mtime_ns)
            self.assertEqual(source.stat().st_size, source_stat.st_size)
            self.assertNotEqual(instance.userdata.resolve(), source.resolve())
            self.assertTrue(instance.metadata.is_file())
            payload = json.loads(instance.metadata.read_text(encoding="utf-8"))
            self.assertEqual(payload["product_out"], str(product.product_out))

    def test_existing_instance_userdata_persists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            source = product.userdata
            assert source is not None
            source_bytes = source.read_bytes()
            work = root / "state"
            instance = prepare_instance(product, work_dir=work, environ={})
            assert instance.userdata is not None
            instance.userdata.write_bytes(b"guest-persistent-data")

            prepared_again = prepare_instance(product, work_dir=work, environ={})

            assert prepared_again.userdata is not None
            self.assertEqual(prepared_again.userdata.read_bytes(), b"guest-persistent-data")
            self.assertEqual(source.read_bytes(), source_bytes)

    def test_existing_userdata_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            source = product.userdata
            assert source is not None
            work = root / "state"
            dest = work / "instances" / "emu64x" / "userdata.img"
            dest.parent.mkdir(parents=True)
            try:
                os.symlink(source, dest)
            except OSError as exc:
                raise unittest.SkipTest(f"symlink not permitted: {exc}") from exc
            with self.assertRaises(UserdataError):
                prepare_instance(product, work_dir=work, environ={})

    def test_existing_userdata_hard_link_to_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            source = product.userdata
            assert source is not None
            work = root / "state"
            dest = work / "instances" / "emu64x" / "userdata.img"
            dest.parent.mkdir(parents=True)
            try:
                os.link(source, dest)
            except OSError as exc:
                raise unittest.SkipTest(f"hard links not permitted: {exc}") from exc

            with self.assertRaises(UserdataError):
                prepare_instance(product, work_dir=work, environ={})

    def test_existing_userdata_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            work = root / "state"
            dest = work / "instances" / "emu64x" / "userdata.img"
            dest.mkdir(parents=True)
            with self.assertRaises(UserdataError):
                prepare_instance(product, work_dir=work, environ={})

    def test_reset_refuses_product_out(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            work = root / "state"
            work.mkdir()
            with self.assertRaises(UserdataError):
                reset_instance_userdata(
                    product.userdata or product_dir / "userdata.img",
                    state_root=work,
                    product_out=product.product_out,
                    instance_dir=work / "instances" / "emu64x",
                )

    def test_reset_refuses_dotdot_and_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            work = root / "state"
            work.mkdir()
            with self.assertRaises(UserdataError):
                reset_instance_userdata(
                    Path("foo/../../etc/passwd"),
                    state_root=work,
                    product_out=product_dir,
                )
            with self.assertRaises(UserdataError):
                reset_instance_userdata(
                    (root / "startloader-reset-absolute").resolve(),
                    state_root=work,
                    product_out=product_dir,
                )

    def test_reset_refuses_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            work = root / "state"
            work.mkdir()
            with self.assertRaises(UserdataError):
                reset_instance_userdata(
                    work,
                    state_root=work,
                    product_out=product_dir,
                    instance_dir=work,
                )

    def test_reset_refuses_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            work = root / "state"
            inst = work / "instances" / "emu64x"
            inst.mkdir(parents=True)
            target = product_dir / "userdata.img"
            link = inst / "userdata.img"
            try:
                os.symlink(target, link)
            except OSError as exc:
                raise unittest.SkipTest(f"symlink not permitted: {exc}") from exc
            with self.assertRaises(UserdataError):
                reset_instance_userdata(
                    link,
                    state_root=work,
                    product_out=product_dir,
                    instance_dir=inst,
                )
            self.assertTrue(target.is_file())

    def test_reset_deletes_instance_copy_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product_dir = make_ranchu_product(root / "product", "emu64x")
            product = inventory_product_out(product_dir)
            work = root / "state"
            instance = prepare_instance(product, work_dir=work, environ={})
            assert instance.userdata is not None
            self.assertTrue(instance.userdata.is_file())
            source = product.userdata
            assert source is not None
            source_bytes = source.read_bytes()
            instance.userdata.write_bytes(b"guest-mutated-data")
            reset = prepare_instance(product, work_dir=work, reset_data=True, environ={})
            self.assertTrue(source.is_file())
            self.assertTrue(instance.userdata.is_file())  # recopied after reset
            assert reset.userdata is not None
            self.assertEqual(reset.userdata.read_bytes(), source_bytes)
            self.assertEqual(source.read_bytes(), source_bytes)


if __name__ == "__main__":
    unittest.main()
