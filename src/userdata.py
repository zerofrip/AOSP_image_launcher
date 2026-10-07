"""Instance userdata under the platform state directory. PRODUCT_OUT stays immutable."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from errors import UserdataError
from models import ProductArtifacts
from paths import canonical_path, detect_wsl, sanitize_instance_name, symlink_info

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class InstancePaths:
    state_root: Path
    instance_dir: Path
    userdata: Path | None
    metadata: Path


def default_state_dir(
    *,
    environ: Mapping[str, str] | None = None,
    is_windows: bool | None = None,
    work_dir: str | Path | None = None,
) -> Path:
    env = dict(environ) if environ is not None else dict(os.environ)
    if work_dir is not None:
        text = os.fspath(work_dir).strip()
        if not text:
            raise UserdataError("work-dir is empty")
        _reject_dotdot(text)
        return Path(text).expanduser()
    override = env.get("STARTLOADER_STATE_DIR", "").strip()
    if override:
        _reject_dotdot(override)
        return Path(override).expanduser()
    windows = is_windows
    if windows is None:
        windows = os.name == "nt" and not detect_wsl(env)
    if windows:
        base = env.get("LOCALAPPDATA", "").strip()
        if not base:
            raise UserdataError("LOCALAPPDATA is not set; cannot choose a Windows state directory")
        return Path(base) / "startloader"
    xdg = env.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "startloader"
    home = env.get("HOME", "").strip() or str(Path.home())
    return Path(home) / ".local" / "state" / "startloader"


def instance_name_for(target: str) -> str:
    try:
        return sanitize_instance_name(target)
    except ValueError:
        cleaned = _UNSAFE.sub("_", target).strip("._-")
        if not cleaned:
            cleaned = "instance"
        return sanitize_instance_name(cleaned)


def prepare_instance(
    product: ProductArtifacts,
    *,
    work_dir: str | Path | None = None,
    reset_data: bool = False,
    read_only: bool = False,
    environ: Mapping[str, str] | None = None,
    source_userdata: Path | None = None,
) -> InstancePaths:
    """Seed persistent instance userdata. Never writes under PRODUCT_OUT."""

    env = dict(environ) if environ is not None else dict(os.environ)
    state_root = canonical_path(default_state_dir(environ=env, work_dir=work_dir))
    _assert_not_product_out(state_root, product.product_out)
    name = instance_name_for(product.target_name or product.product_out.name)
    instance_dir = _safe_instance_dir(state_root, name, product_out=product.product_out)
    instance_dir.mkdir(parents=True, exist_ok=True)
    dest = instance_dir / "userdata.img"
    meta = instance_dir / "state.json"
    src = source_userdata or product.userdata
    if reset_data:
        reset_instance_userdata(
            dest,
            state_root=state_root,
            product_out=product.product_out,
            instance_dir=instance_dir,
        )
    userdata_path: Path | None = dest if src is not None else None
    if src is not None and not read_only:
        if not src.is_file():
            raise UserdataError(f"userdata source is not a file: {src}")
        _assert_source_not_replaced(src, dest, product.product_out)
        if not _existing_userdata_is_safe(dest, src):
            _atomic_copy(src, dest)
        userdata_path = dest
    elif src is not None and read_only:
        userdata_path = src
    _write_state(
        meta,
        {
            "target": product.target_name,
            "product_out": str(product.product_out),
            "userdata_source": str(src) if src is not None else None,
            "userdata": str(userdata_path) if userdata_path is not None else None,
            "read_only": read_only,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return InstancePaths(
        state_root=state_root,
        instance_dir=instance_dir,
        userdata=userdata_path,
        metadata=meta,
    )


def reset_instance_userdata(
    path: Path,
    *,
    state_root: Path,
    product_out: Path,
    instance_dir: Path | None = None,
) -> None:
    """Delete instance userdata only. Refuses unsafe paths."""

    raw = os.fspath(path)
    _reject_dotdot(raw)
    if Path(raw).is_absolute() and instance_dir is None:
        # Absolute user-supplied wipe targets are refused; computed dests pass instance_dir.
        raise UserdataError("refusing to reset an absolute path without an instance directory")
    dest = Path(raw)
    state_res = canonical_path(state_root)
    product_res = canonical_path(product_out)
    dest_res = canonical_path(dest)
    if dest_res == state_res:
        raise UserdataError("refusing to delete the state root")
    if dest_res == product_res or _is_relative_to(dest_res, product_res):
        raise UserdataError("refusing to delete PRODUCT_OUT")
    if not _is_relative_to(dest_res, state_res / "instances") and not _is_relative_to(dest_res, state_res):
        raise UserdataError(f"reset path is not under the state directory: {dest}")
    is_link, target, _broken = symlink_info(dest)
    if is_link:
        raise UserdataError(
            f"refusing to follow a symlink during reset: {dest} -> {target}"
        )
    inst = canonical_path(instance_dir) if instance_dir is not None else dest_res.parent
    if inst == state_res:
        raise UserdataError("refusing to delete the state root")
    if inst == product_res or _is_relative_to(inst, product_res):
        raise UserdataError("refusing to delete PRODUCT_OUT")
    if not _is_relative_to(inst, state_res):
        raise UserdataError("instance directory escapes the state root")
    if dest.exists() and dest.is_dir():
        raise UserdataError("refusing to recursively delete a directory")
    try:
        dest.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UserdataError(f"failed to reset userdata: {exc}") from exc


def _safe_instance_dir(state_root: Path, name: str, *, product_out: Path) -> Path:
    _reject_dotdot(name)
    if Path(name).is_absolute():
        raise UserdataError("instance name must not be an absolute path")
    instance_dir = state_root / "instances" / name
    resolved = canonical_path(instance_dir)
    state_res = canonical_path(state_root)
    product_res = canonical_path(product_out)
    if resolved == state_res:
        raise UserdataError("instance directory must not be the state root")
    if resolved == product_res or _is_relative_to(resolved, product_res):
        raise UserdataError("instance directory must not be inside PRODUCT_OUT")
    if not _is_relative_to(resolved, state_res):
        raise UserdataError("instance directory escapes the state root (symlink?)")
    is_link, target, broken = symlink_info(instance_dir)
    if is_link:
        raise UserdataError(
            f"refusing instance directory symlink {instance_dir} -> {target}"
            + (" (broken)" if broken else "")
        )
    return instance_dir


def _assert_not_product_out(state_root: Path, product_out: Path) -> None:
    state_res = canonical_path(state_root)
    product_res = canonical_path(product_out)
    if state_res == product_res or _is_relative_to(state_res, product_res):
        raise UserdataError("state directory must not be PRODUCT_OUT")


def _assert_source_not_replaced(src: Path, dest: Path, product_out: Path) -> None:
    src_res = canonical_path(src)
    dest_res = canonical_path(dest)
    product_res = canonical_path(product_out)
    if dest_res == src_res:
        raise UserdataError("refusing to overwrite the PRODUCT_OUT userdata in place")
    if _is_relative_to(dest_res, product_res):
        raise UserdataError("refusing to write userdata inside PRODUCT_OUT")


def _existing_userdata_is_safe(dest: Path, src: Path) -> bool:
    """Return whether a safe regular destination already exists."""

    is_link, target, broken = symlink_info(dest)
    if is_link:
        raise UserdataError(
            f"refusing existing userdata symlink {dest} -> {target}"
            + (" (broken)" if broken else "")
        )
    if not dest.exists():
        return False
    if not dest.is_file():
        raise UserdataError(f"existing userdata is not a regular file: {dest}")
    try:
        same_file = os.path.samefile(src, dest)
    except OSError as exc:
        raise UserdataError(f"failed to validate existing userdata: {exc}") from exc
    if same_file:
        raise UserdataError("refusing instance userdata linked to the source userdata")
    return True


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".userdata-", suffix=".tmp", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_state(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            Path(tmp_name).unlink()
        except OSError:
            pass
        raise


def _reject_dotdot(raw: str) -> None:
    parts = Path(raw).parts
    if ".." in parts:
        raise UserdataError(f"refusing path containing '..': {raw}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
