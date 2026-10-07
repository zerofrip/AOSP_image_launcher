"""AOSP build orchestration tests. No real build or network is used."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from aosp_builder import build_aosp_product, query_product_out
from errors import AospBuildError


class FakeRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((list(argv), kwargs))
        return next(self.results)


def _aosp_root(root: Path) -> Path:
    (root / "build" / "soong").mkdir(parents=True)
    (root / "build" / "envsetup.sh").write_text("# mock\n", encoding="utf-8")
    (root / "build" / "soong" / "soong_ui.bash").write_text("# mock\n", encoding="utf-8")
    return root


class AospBuilderTests(unittest.TestCase):
    def test_build_queries_product_out_then_runs_droid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _aosp_root(Path(raw))
            product_out = root / "out" / "target" / "product" / "emu64x"
            product_out.mkdir(parents=True)
            runner = FakeRunner(
                [
                    subprocess.CompletedProcess([], 0, str(product_out) + "\n", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                ]
            )
            result = build_aosp_product(
                root,
                runner=runner,
                bash="/bin/bash",
                which=lambda name: f"/usr/bin/{name}",
            )
            self.assertEqual(result.product_out, product_out.resolve())
            self.assertEqual(result.targets, ("droid",))
            self.assertEqual(len(runner.calls), 2)
            query_argv, query_kwargs = runner.calls[0]
            build_argv, build_kwargs = runner.calls[1]
            self.assertEqual(query_argv[0:2], ["/bin/bash", "-c"])
            self.assertTrue(query_kwargs["capture_output"])
            self.assertFalse(query_kwargs["shell"])
            self.assertIn("sdk_phone64_x86_64-trunk_staging-userdebug", build_argv)
            self.assertIn("-j4", build_argv)
            self.assertEqual(build_argv[-1], "droid")
            self.assertFalse(build_kwargs["shell"])

    def test_query_rejects_product_out_outside_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _aosp_root(Path(raw) / "aosp")
            outside = Path(raw) / "elsewhere"
            runner = FakeRunner(
                [subprocess.CompletedProcess([], 0, str(outside) + "\n", "")]
            )
            with self.assertRaises(AospBuildError):
                query_product_out(
                    root,
                    lunch_target="sdk_phone64_x86_64-trunk_staging-userdebug",
                    runner=runner,
                    bash="/bin/bash",
                )

    def test_invalid_lunch_and_build_target_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _aosp_root(Path(raw))
            with self.assertRaises(AospBuildError):
                query_product_out(root, lunch_target="bad;target", bash="/bin/bash")
            with self.assertRaises(AospBuildError):
                build_aosp_product(
                    root,
                    targets=("--ninja-arg",),
                    runner=FakeRunner([]),
                    bash="/bin/bash",
                    which=lambda name: f"/usr/bin/{name}",
                )

    def test_missing_zip_fails_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = _aosp_root(Path(raw))
            with self.assertRaises(AospBuildError) as ctx:
                build_aosp_product(
                    root,
                    runner=FakeRunner([]),
                    bash="/bin/bash",
                    which=lambda _name: None,
                )
            self.assertIn("zip", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
