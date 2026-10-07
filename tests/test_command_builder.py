"""Command formatting tests. Execution always uses argv lists, never shell=True."""

from __future__ import annotations

import subprocess
import unittest

import support  # noqa: F401

from command_builder import format_command, format_command_redacted, redact_argv


class CommandBuilderTests(unittest.TestCase):
    def test_posix_join(self) -> None:
        formatted = format_command(["emulator", "-sysdir", "prod uct"], windows=False)
        self.assertIn("emulator", formatted)
        self.assertIn("prod uct", formatted)
        self.assertTrue("'" in formatted or "\\" in formatted)

    def test_windows_list2cmdline(self) -> None:
        argv = ["C:\\emu\\emulator.exe", "-sysdir", "C:\\prod uct"]
        formatted = format_command(argv, windows=True)
        self.assertEqual(formatted, subprocess.list2cmdline(argv))
        self.assertIn("emulator.exe", formatted)

    def test_redact_secrets(self) -> None:
        argv = [
            "emulator",
            "--password",
            "s3cret",
            "-prop",
            "token=abc",
            "plain",
        ]
        redacted = redact_argv(argv)
        self.assertEqual(redacted[2], "<redacted>")
        self.assertTrue(redacted[4].endswith("=<redacted>") or "token=<redacted>" in redacted)
        self.assertNotIn("s3cret", redacted)
        self.assertNotIn("abc", " ".join(redacted))
        self.assertIn("plain", redacted)
        text = format_command_redacted(argv, windows=False)
        self.assertNotIn("s3cret", text)

    def test_format_does_not_use_shell(self) -> None:
        # format_command is display-only; a metacharacter must stay in one argv slot.
        formatted = format_command(["echo", "a && b"], windows=False)
        self.assertIn("a && b", formatted)


if __name__ == "__main__":
    unittest.main()
