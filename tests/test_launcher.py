"""CLI tests. Diagnose/dry-run must not spawn a guest."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401
from support import make_aosp_tree, make_cuttlefish_product, make_ranchu_product

import launcher
from aosp_builder import AospBuildResult


class LauncherCliTests(unittest.TestCase):
    def test_help(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            launcher.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = buf.getvalue()
        self.assertIn("--product-out", text)
        self.assertIn("--diagnose", text)
        self.assertIn("--build-aosp", text)

    def test_build_only_validates_generated_ranchu_product(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            product = make_ranchu_product(root / "out" / "target" / "product", "emu64x")
            result = AospBuildResult(
                root=root,
                lunch_target="sdk_phone64_x86_64-trunk_staging-userdebug",
                product_out=product,
                targets=("droid",),
                jobs=4,
                argv=("bash",),
            )
            buf = io.StringIO()
            with patch("launcher.build_aosp_product", return_value=result) as build_mock:
                with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                    code = launcher.main(["--build-aosp", str(root), "--build-only"])
            self.assertEqual(code, 0)
            self.assertIn("Validated complete", buf.getvalue())
            build_mock.assert_called_once()

    def test_diagnose_ranchu_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                code = launcher.main(["--product-out", str(product), "--diagnose"])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("emulator_ranchu", out)
            self.assertIn("x86_64", out)
            self.assertIn("bootable:", out)

    def test_diagnose_cuttlefish_exit_0_recommends_launch_cvd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_cuttlefish_product(Path(raw), "vsoc_x86_64")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = launcher.main(["--product-out", str(product), "--diagnose"])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("cuttlefish", out)
            self.assertIn("launch_cvd", out)
            self.assertIn("x86_64", out)
            self.assertIn("dynamic_partitions: True", out)
            self.assertIn("No executable launch plan", out)

    def test_dry_run_cuttlefish_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_cuttlefish_product(Path(raw), "vsoc_x86_64")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = launcher.main(["--product-out", str(product), "--dry-run"])
            self.assertNotEqual(code, 0)
            self.assertIn("bootable:      False", buf.getvalue())

    def test_extra_arg_requires_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = launcher.main(
                    ["--product-out", str(product), "--dry-run", "--extra-arg=-gpu"]
                )
            self.assertNotEqual(code, 0)
            self.assertIn("allow-extra-args", err.getvalue())

    def test_list_product_outs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tree = make_aosp_tree(Path(raw), ["emu64x", "vsoc_x86_64"])
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = launcher.main(["--list-product-outs", str(tree)])
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn("emu64x", text)
            self.assertIn("vsoc_x86_64", text)

    def test_missing_product_out_nonzero(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = launcher.main(["--product-out", "/no/such/product_out_startloader", "--diagnose"])
        self.assertNotEqual(code, 0)
        self.assertIn("error:", err.getvalue())

    def test_adb_port_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_ranchu_product(Path(raw), "emu64x")
            for port in (0, 65536):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = launcher.main(
                        ["--product-out", str(product), "--adb-port", str(port), "--dry-run"]
                    )
                self.assertNotEqual(code, 0, msg=f"port {port} should fail")
                self.assertIn("error:", err.getvalue())

    def test_dry_run_prints_warnings_and_argv_elements(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            product = make_cuttlefish_product(Path(raw), "vsoc_x86_64")
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                code = launcher.main(["--product-out", str(product), "--dry-run"])
            self.assertNotEqual(code, 0)
            out = buf.getvalue()
            combined = out + err.getvalue()
            self.assertIn("argv=[]", out)
            self.assertIn("backend:", out)
            self.assertTrue("warning:" in combined or "error:" in combined)
            self.assertIn("bootable:      False", out)

    def test_memory_legacy_4g(self) -> None:
        parser = launcher.build_parser()
        args = parser.parse_args(["--memory", "4G", "--diagnose"])
        self.assertEqual(args.memory, 4096)


if __name__ == "__main__":
    unittest.main()
