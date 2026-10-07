"""Safe orchestration for building an x86_64 AOSP Emulator PRODUCT_OUT."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from errors import AospBuildError
from paths import canonical_path

Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]

_LUNCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:+-]*$")
_QUERY_SCRIPT = """\
set -euo pipefail
source build/envsetup.sh >/dev/null
lunch "$1" >/dev/null
get_build_var PRODUCT_OUT
"""
_BUILD_SCRIPT = """\
set -euo pipefail
source build/envsetup.sh >/dev/null
lunch "$1"
shift
m "$@"
"""


@dataclass(frozen=True)
class AospBuildResult:
    root: Path
    lunch_target: str
    product_out: Path
    targets: tuple[str, ...]
    jobs: int
    argv: tuple[str, ...]


def build_aosp_product(
    root: str | Path,
    *,
    lunch_target: str = "sdk_phone64_x86_64-trunk_staging-userdebug",
    targets: Sequence[str] = ("droid",),
    jobs: int = 4,
    runner: Runner = subprocess.run,
    bash: str | Path | None = None,
    which: Which = shutil.which,
) -> AospBuildResult:
    """Run an AOSP build and return its validated PRODUCT_OUT."""

    aosp_root = _validate_root(root)
    lunch = _validate_lunch(lunch_target)
    selected_targets = _validate_targets(targets)
    if not (1 <= int(jobs) <= 256):
        raise AospBuildError("--build-jobs must be in 1..256")
    bash_path = os.fspath(bash) if bash is not None else which("bash")
    if not bash_path:
        raise AospBuildError("bash was not found; AOSP envsetup.sh requires bash")
    if not which("zip"):
        raise AospBuildError(
            "required AOSP host tool 'zip' was not found on PATH; install the OS zip package",
            exit_code=1,
        )

    product_out = query_product_out(
        aosp_root,
        lunch_target=lunch,
        runner=runner,
        bash=bash_path,
    )
    argv = (
        os.fspath(bash_path),
        "-c",
        _BUILD_SCRIPT,
        "startloader-aosp-build",
        lunch,
        f"-j{int(jobs)}",
        *selected_targets,
    )
    try:
        completed = runner(list(argv), cwd=aosp_root, check=False, shell=False)
    except FileNotFoundError as exc:
        raise AospBuildError(f"failed to start AOSP build: {exc}", exit_code=1) from exc
    if completed.returncode != 0:
        raise AospBuildError(
            f"AOSP build failed for {lunch} (exit {completed.returncode})",
            exit_code=int(completed.returncode or 1),
        )
    if not product_out.is_dir():
        raise AospBuildError(f"build succeeded but PRODUCT_OUT is missing: {product_out}")
    return AospBuildResult(
        root=aosp_root,
        lunch_target=lunch,
        product_out=product_out,
        targets=selected_targets,
        jobs=int(jobs),
        argv=argv,
    )


def query_product_out(
    root: str | Path,
    *,
    lunch_target: str,
    runner: Runner = subprocess.run,
    bash: str | Path | None = None,
) -> Path:
    """Ask the selected lunch configuration for PRODUCT_OUT."""

    aosp_root = _validate_root(root)
    lunch = _validate_lunch(lunch_target)
    bash_path = os.fspath(bash) if bash is not None else shutil.which("bash")
    if not bash_path:
        raise AospBuildError("bash was not found; AOSP envsetup.sh requires bash")
    argv = [
        os.fspath(bash_path),
        "-c",
        _QUERY_SCRIPT,
        "startloader-aosp-query",
        lunch,
    ]
    try:
        completed = runner(
            argv,
            cwd=aosp_root,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AospBuildError(f"failed to query PRODUCT_OUT: {exc}", exit_code=1) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown lunch error").strip()
        raise AospBuildError(f"lunch {lunch} failed: {detail}", exit_code=1)
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise AospBuildError(f"lunch {lunch} returned an empty PRODUCT_OUT")
    raw = Path(lines[-1]).expanduser()
    product_out = canonical_path(raw if raw.is_absolute() else aosp_root / raw)
    product_root = canonical_path(aosp_root / "out" / "target" / "product")
    if not _is_relative_to(product_out, product_root):
        raise AospBuildError(
            f"refusing PRODUCT_OUT outside the AOSP product directory: {product_out}"
        )
    return product_out


def _validate_root(root: str | Path) -> Path:
    text = os.fspath(root).strip()
    if not text:
        raise AospBuildError("AOSP root is empty")
    resolved = canonical_path(Path(text).expanduser())
    if not resolved.is_dir():
        raise AospBuildError(f"AOSP root is not a directory: {resolved}")
    required = (resolved / "build" / "envsetup.sh", resolved / "build" / "soong" / "soong_ui.bash")
    missing = [os.fspath(path.relative_to(resolved)) for path in required if not path.is_file()]
    if missing:
        raise AospBuildError("not an AOSP source root; missing " + ", ".join(missing))
    return resolved


def _validate_lunch(value: str) -> str:
    lunch = value.strip()
    if not _LUNCH_RE.fullmatch(lunch):
        raise AospBuildError(f"invalid lunch target: {value!r}")
    return lunch


def _validate_targets(values: Sequence[str]) -> tuple[str, ...]:
    targets = tuple(value.strip() for value in values)
    if not targets:
        targets = ("droid",)
    for target in targets:
        if not _TARGET_RE.fullmatch(target) or target.startswith("-"):
            raise AospBuildError(f"invalid AOSP build target: {target!r}")
    return targets


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
