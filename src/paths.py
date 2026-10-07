"""Host path helpers: WSL conversion, canonicalization, symlink notes."""

from __future__ import annotations

import os
import re
from pathlib import Path

_WIN_ABS = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_UNSAFE_TARGET = re.compile(r"[^A-Za-z0-9._-]")


def detect_wsl(environ: dict[str, str] | None = None, osrelease_text: str | None = None) -> bool:
    env = environ if environ is not None else os.environ
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    text = osrelease_text
    if text is None:
        for candidate in ("/proc/sys/kernel/osrelease", "/proc/version"):
            try:
                text = Path(candidate).read_text(encoding="utf-8", errors="replace")
                break
            except OSError:
                continue
    if text and "microsoft" in text.lower():
        return True
    return False


def windows_to_wsl(raw: str) -> str:
    text = raw.strip().strip('"')
    match = _WIN_ABS.match(text)
    if not match:
        return text
    drive, rest = match.group(1).lower(), match.group(2)
    rest = rest.replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def normalize_user_path(
    raw: str,
    *,
    cwd: Path | None = None,
    is_wsl: bool | None = None,
    environ: dict[str, str] | None = None,
) -> Path:
    text = raw.strip().strip('"')
    if is_wsl is None:
        is_wsl = detect_wsl(environ)
    if is_wsl:
        text = windows_to_wsl(text)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    return path


def canonical_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def symlink_info(path: Path) -> tuple[bool, str | None, bool]:
    try:
        is_link = path.is_symlink()
    except OSError:
        return False, None, False
    if not is_link:
        return False, None, False
    try:
        target = os.readlink(path)
    except OSError:
        target = None
    try:
        broken = not path.exists()
    except OSError:
        broken = True
    return True, target, broken


def sanitize_instance_name(name: str) -> str:
    if not name or name in {".", ".."}:
        raise ValueError(f"invalid instance name: {name!r}")
    if name.startswith("/") or name.startswith("\\") or ":" in name:
        raise ValueError(f"instance name must not be an absolute path: {name!r}")
    if ".." in name.split("/") or ".." in name.split("\\"):
        raise ValueError(f"instance name must not contain '..': {name!r}")
    if _UNSAFE_TARGET.search(name):
        raise ValueError(
            f"instance name may contain only letters, digits, '.', '_' and '-': {name!r}"
        )
    return name
