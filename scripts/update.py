"""Safely fast-forward this checkout from its configured upstream."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

Runner = Callable[..., subprocess.CompletedProcess[str]]
REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(
    args: Sequence[str],
    *,
    runner: Runner,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "unknown git error").strip()


def update_repo(*, runner: Runner = subprocess.run, repo_root: Path = REPO_ROOT) -> int:
    """Fast-forward a clean checkout using its configured upstream."""

    print("Checking for updates...")
    try:
        inside = _git(["rev-parse", "--is-inside-work-tree"], runner=runner, repo_root=repo_root)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            print(f"Error: not a Git work tree: {_failure(inside)}", file=sys.stderr)
            return 1

        status = _git(
            ["status", "--porcelain", "--untracked-files=normal"],
            runner=runner,
            repo_root=repo_root,
        )
        if status.returncode != 0:
            print(f"Error: could not inspect the work tree: {_failure(status)}", file=sys.stderr)
            return 1
        if status.stdout.strip():
            print("Error: refusing to update a dirty work tree.", file=sys.stderr)
            return 2

        upstream = _git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            runner=runner,
            repo_root=repo_root,
        )
        if upstream.returncode != 0 or not upstream.stdout.strip():
            print("Error: current branch has no configured upstream.", file=sys.stderr)
            return 2

        upstream_name = upstream.stdout.strip()
        print(f"Updating from {upstream_name} (fast-forward only)...")
        result = _git(["pull", "--ff-only"], runner=runner, repo_root=repo_root)
        if result.returncode != 0:
            print(f"Error during update: {_failure(result)}", file=sys.stderr)
            return 1
        output = result.stdout.strip()
        print(output if output else "Update completed successfully.")
        return 0
    except FileNotFoundError:
        print("Error: 'git' command not found. Please install Git to use the update script.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(update_repo())
