"""Tests for the fail-closed repository updater (no network access)."""

from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from pathlib import Path

from scripts import update


def completed(argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))
        return next(self.results)


class UpdateTests(unittest.TestCase):
    def run_update(self, runner: FakeRunner) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        repo = Path("/tmp/example-checkout")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = update.update_repo(runner=runner, repo_root=repo)
        for _argv, kwargs in runner.calls:
            self.assertEqual(kwargs["cwd"], repo)
            self.assertFalse(kwargs["shell"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_clean_checkout_pulls_configured_upstream_fast_forward_only(self) -> None:
        runner = FakeRunner(
            [
                completed([], stdout="true\n"),
                completed([], stdout=""),
                completed([], stdout="origin/startloader\n"),
                completed([], stdout="Already up to date.\n"),
            ]
        )
        code, stdout, stderr = self.run_update(runner)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("origin/startloader", stdout)
        self.assertEqual(runner.calls[-1][0], ["git", "pull", "--ff-only"])
        self.assertNotIn("main", " ".join(runner.calls[-1][0]))

    def test_dirty_checkout_is_refused_before_upstream_or_pull(self) -> None:
        runner = FakeRunner(
            [completed([], stdout="true\n"), completed([], stdout="?? local.txt\n")]
        )
        code, _stdout, stderr = self.run_update(runner)
        self.assertEqual(code, 2)
        self.assertIn("dirty", stderr)
        self.assertEqual(len(runner.calls), 2)

    def test_missing_upstream_is_refused_without_pull(self) -> None:
        runner = FakeRunner(
            [
                completed([], stdout="true\n"),
                completed([], stdout=""),
                completed([], returncode=128, stderr="no upstream configured\n"),
            ]
        )
        code, _stdout, stderr = self.run_update(runner)
        self.assertEqual(code, 2)
        self.assertIn("no configured upstream", stderr)
        self.assertEqual(len(runner.calls), 3)


if __name__ == "__main__":
    unittest.main()
