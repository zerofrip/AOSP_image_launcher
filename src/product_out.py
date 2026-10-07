"""Resolve AOSP PRODUCT_OUT directories and inventory launch artifacts.

PRODUCT_OUT is treated as read-only. This module never creates, modifies,
deletes, or remounts files under a product directory.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from errors import AmbiguousProductOutError, ArchitectureError, ProductOutError
from image_inspector import inspect_file
from models import Artifact, Evidence, ProductArtifacts, ValidationIssue
from paths import canonical_path, detect_wsl, normalize_user_path, symlink_info

KERNEL_CANDIDATES: tuple[str, ...] = (
    "kernel-ranchu-64",
    "kernel-ranchu",
    "kernel",
    "bzImage",
    "Image",
)
RAMDISK_CANDIDATES: tuple[str, ...] = (
    "ramdisk-qemu.img",
    "ramdisk.img",
    "initramfs.img",
)

_BOOT_NAME = "boot.img"
_VENDOR_BOOT_NAME = "vendor_boot.img"
_INIT_BOOT_NAME = "init_boot.img"
_SUPER_NAME = "super.img"

_PARTITION_FILES: tuple[tuple[str, str], ...] = (
    ("system", "system.img"),
    ("system_ext", "system_ext.img"),
    ("vendor", "vendor.img"),
    ("product", "product.img"),
    ("userdata", "userdata.img"),
)
_OPTIONAL_FILES: tuple[tuple[str, str], ...] = (
    ("cache", "cache.img"),
    ("metadata", "metadata.img"),
    ("odm", "odm.img"),
    ("odm_dlkm", "odm_dlkm.img"),
    ("vendor_dlkm", "vendor_dlkm.img"),
    ("system_dlkm", "system_dlkm.img"),
    ("system_other", "system_other.img"),
    ("system_qemu", "system-qemu.img"),
    ("vendor_qemu", "vendor-qemu.img"),
    ("encryptionlist", "encryptionlist.img"),
)

_BUILD_PROP_RELATIVE: tuple[str, ...] = (
    "system/build.prop",
    "vendor/build.prop",
    "product/etc/build.prop",
    "odm/etc/build.prop",
    "build.prop",
    "system/etc/prop.default",
    "default.prop",
)

_PRODUCT_MARKERS: frozenset[str] = frozenset(
    {
        "build.prop",
        "system.img",
        "vendor.img",
        "super.img",
        "kernel",
        "kernel-ranchu",
        "kernel-ranchu-64",
        "bzImage",
        "advancedFeatures.ini",
        "android-info.txt",
        "required_images",
        "ramdisk.img",
        "ramdisk-qemu.img",
        "boot.img",
        "vendor_boot.img",
        "userdata.img",
        "misc_info.txt",
    }
)

_ABI_PRIMARY_KEYS: tuple[str, ...] = (
    "ro.product.cpu.abi",
    "ro.product.vendor.cpu.abi",
    "ro.vendor.product.cpu.abi",
    "ro.bionic.arch",
)
_ABI_LIST_KEYS: tuple[str, ...] = (
    "ro.product.cpu.abilist",
    "ro.product.cpu.abilist64",
)
_HARDWARE_KEYS: tuple[str, ...] = ("ro.hardware", "ro.boot.hardware")
_MODEL_KEYS: tuple[str, ...] = (
    "ro.product.model",
    "ro.product.vendor.model",
    "ro.product.system.model",
)
_BOARD_KEYS: tuple[str, ...] = ("ro.product.board", "ro.product.vendor.board")
_NAME_KEYS: tuple[str, ...] = (
    "ro.product.name",
    "ro.product.vendor.name",
    "ro.product.device",
    "ro.product.vendor.device",
)

_X86_64_RE = re.compile(r"x86[_-]64|amd64|emu64x", re.IGNORECASE)
_ARM64_RE = re.compile(r"aarch64|arm64", re.IGNORECASE)
_ARM32_RE = re.compile(r"armeabi|armv7", re.IGNORECASE)
_X86_32_RE = re.compile(r"(?:^|[^a-z0-9])x86(?:$|[^_a-z0-9-])", re.IGNORECASE)

_PARENT_WALK_LIMIT = 48


@dataclass(frozen=True)
class ResolvedProductOut:
    """Canonical PRODUCT_OUT plus the original request string for diagnostics."""

    path: Path
    requested_path: str
    source: str
    is_symlink: bool = False
    symlink_target: str | None = None
    broken_symlink: bool = False

    def __fspath__(self) -> str:
        return os.fspath(self.path)


def resolve_product_out(
    explicit: str | Path | None,
    *,
    target: str | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    is_wsl: bool | None = None,
) -> ResolvedProductOut:
    """Resolve PRODUCT_OUT with --product-out > PRODUCT_OUT > unique discovery.

    Discovery uses ANDROID_PRODUCT_OUT when set, otherwise cwd and parents
    that contain ``out/target/product``. Multiple matches raise
    :class:`AmbiguousProductOutError` instead of picking interactively.
    ``--target`` filters by directory name.
    """

    env = _mapping(environ)
    workdir = _as_path(cwd) if cwd is not None else Path.cwd()
    wsl = detect_wsl(env) if is_wsl is None else is_wsl
    target_name = _clean_target(target)

    if (raw := _clean_optional(explicit)) is not None:
        return _resolve_given(
            raw,
            target=target_name,
            cwd=workdir,
            is_wsl=wsl,
            environ=env,
            source="explicit",
        )
    if (raw := _clean_optional(env.get("PRODUCT_OUT"))) is not None:
        return _resolve_given(
            raw,
            target=target_name,
            cwd=workdir,
            is_wsl=wsl,
            environ=env,
            source="env:PRODUCT_OUT",
        )
    if (raw := _clean_optional(env.get("ANDROID_PRODUCT_OUT"))) is not None:
        return _resolve_given(
            raw,
            target=target_name,
            cwd=workdir,
            is_wsl=wsl,
            environ=env,
            source="env:ANDROID_PRODUCT_OUT",
        )
    candidates = _discover_from_cwd(workdir, target=target_name)
    return _pick_unique(
        candidates,
        requested_path=str(workdir),
        source="discovered",
        target=target_name,
    )


def list_product_outs(
    root: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    is_wsl: bool | None = None,
) -> list[Path]:
    """List product directories under ``root`` (default: cwd). Never writes."""

    env = _mapping(environ)
    workdir = _as_path(cwd) if cwd is not None else Path.cwd()
    wsl = detect_wsl(env) if is_wsl is None else is_wsl
    if root is None:
        start = workdir
        requested = str(workdir)
    else:
        raw = os.fspath(root).strip().strip('"')
        if not raw:
            start = workdir
            requested = str(workdir)
        else:
            start = normalize_user_path(raw, cwd=workdir, is_wsl=wsl, environ=dict(env))
            requested = raw
            if not _exists(start):
                raise ProductOutError(f"path does not exist: {requested}")
    return _list_under(canonical_path(start))


def inventory_product_out(
    product_out: str | Path | ResolvedProductOut,
    *,
    requested_path: str | None = None,
) -> ProductArtifacts:
    """Inventory artifacts under a resolved PRODUCT_OUT. Read-only.

    Refuses guests that are not x86_64. Conflicting x86_64 vs ARM evidence
    raises :class:`ArchitectureError` instead of guessing.
    """

    resolved = _coerce_resolved(product_out, requested_path=requested_path)
    directory = resolved.path
    if not _is_dir(directory):
        raise ProductOutError(f"PRODUCT_OUT is not a directory: {resolved.requested_path}")
    if not _is_product_dir(directory):
        raise ProductOutError(
            f"not an AOSP PRODUCT_OUT directory: {resolved.requested_path} "
            f"(canonical {directory})"
        )

    issues: list[ValidationIssue] = []
    artifacts: list[Artifact] = []
    properties = _load_properties(directory)
    required_images = _load_required_images(directory)
    android_info = _load_key_values(directory / "android-info.txt")
    fingerprints = _load_fingerprints(directory)

    if resolved.is_symlink:
        issues.append(
            ValidationIssue(
                severity="info",
                code="symlink_product_out",
                message=(
                    f"PRODUCT_OUT is a symlink -> {resolved.symlink_target}"
                    + (" (broken)" if resolved.broken_symlink else "")
                ),
                path=str(directory),
            )
        )

    arch_evidence: list[Evidence] = []
    family_evidence: list[Evidence] = []

    target_name = directory.name
    arch_evidence.append(Evidence("directory_name", "target", target_name))
    _record_arch_token(arch_evidence, "directory_name", "target", target_name)

    _collect_prop_arch_evidence(arch_evidence, properties)
    _collect_fingerprint_arch_evidence(arch_evidence, fingerprints)
    _collect_family_name_evidence(family_evidence, target_name, properties, fingerprints, android_info)

    kernel_path, kernel_artifacts, kernel_arch_evidence = _inventory_kernels(directory)
    artifacts.extend(kernel_artifacts)
    arch_evidence.extend(kernel_arch_evidence)
    for name in ("kernel-ranchu-64", "kernel-ranchu"):
        if _is_file(directory / name):
            family_evidence.append(Evidence("file", name, "present"))

    ramdisk_path, ramdisk_artifacts = _inventory_ramdisks(directory)
    artifacts.extend(ramdisk_artifacts)

    boot_path, boot_art = _optional_image(directory, _BOOT_NAME, "boot", "detected")
    artifacts.extend(boot_art)
    vendor_boot_path, vendor_boot_art = _special_unsupported(
        directory,
        _VENDOR_BOOT_NAME,
        "vendor_boot",
        "never passed as a regular disk or initrd to v1 emulator/QEMU backends",
    )
    artifacts.extend(vendor_boot_art)
    init_boot_path, init_boot_art = _optional_image(
        directory, _INIT_BOOT_NAME, "init_boot", "detected"
    )
    artifacts.extend(init_boot_art)
    super_path, super_art = _special_unsupported(
        directory,
        _SUPER_NAME,
        "super",
        "dynamic-partition super.img is never attached as an ordinary disk",
    )
    artifacts.extend(super_art)

    if _is_file(directory / "advancedFeatures.ini"):
        family_evidence.append(Evidence("file", "advancedFeatures.ini", "present"))
        artifacts.append(
            Artifact(
                name="advancedFeatures.ini",
                kind="config",
                path=directory / "advancedFeatures.ini",
                status="detected",
                format="ini",
            )
        )

    if super_path is not None and vendor_boot_path is not None:
        family_evidence.append(Evidence("file", "super.img+vendor_boot.img", "present"))
    if "super.img" in required_images and "vendor_boot.img" in required_images:
        family_evidence.append(
            Evidence("required_images", "super.img+vendor_boot.img", "listed")
        )

    partition_paths: dict[str, Path | None] = {}
    for kind, filename in _PARTITION_FILES:
        path, items = _optional_image(directory, filename, kind, "detected")
        artifacts.extend(items)
        partition_paths[kind] = path

    optional_images: dict[str, Path] = {}
    for kind, filename in _OPTIONAL_FILES:
        path, items = _optional_image(directory, filename, kind, "optional")
        artifacts.extend(items)
        if path is not None:
            optional_images[kind] = path
    for extra in _extra_root_images(directory, {a.name for a in artifacts}):
        artifacts.append(extra)
        if (
            extra.path is not None
            and extra.status != "unsupported"
            and extra.kind not in optional_images
        ):
            optional_images[extra.kind] = extra.path

    family, family_confidence, family_issues = _classify_family(family_evidence)
    issues.extend(family_issues)

    architecture = _conclude_architecture(arch_evidence)

    ranchu_like = family == "emulator_ranchu" or (
        family == "conflicting" and _has_ranchu_evidence(family_evidence)
    )
    system_path = partition_paths.get("system")
    incomplete = False
    if ranchu_like:
        incomplete = _mark_ranchu_completeness(
            artifacts,
            issues,
            kernel=kernel_path,
            ramdisk=ramdisk_path,
            system=system_path,
            directory=directory,
        )
        _promote_status(artifacts, "kernel", "required" if kernel_path else "missing")
        _promote_status(artifacts, "ramdisk", "required" if ramdisk_path else "missing")
        _promote_status(artifacts, "system", "required" if system_path else "missing")

    dynamic_partitions = super_path is not None or _truthy(
        properties.get("use_dynamic_partitions")
    )
    if dynamic_partitions:
        issues.append(
            ValidationIssue(
                severity="warning",
                code="dynamic_partitions",
                message="dynamic partitions / super.img present; v1 backends cannot map this as a regular disk",
                path=str(super_path) if super_path else None,
            )
        )

    return ProductArtifacts(
        product_out=directory,
        requested_path=resolved.requested_path,
        target_name=target_name,
        architecture=architecture,
        family=family,
        family_confidence=family_confidence,
        architecture_evidence=arch_evidence,
        family_evidence=family_evidence,
        properties=properties,
        kernel=kernel_path,
        ramdisk=ramdisk_path,
        boot=boot_path,
        vendor_boot=vendor_boot_path,
        init_boot=init_boot_path,
        system=system_path,
        system_ext=partition_paths.get("system_ext"),
        vendor=partition_paths.get("vendor"),
        product=partition_paths.get("product"),
        userdata=partition_paths.get("userdata"),
        super_image=super_path,
        optional_images=optional_images,
        artifacts=artifacts,
        issues=issues,
        dynamic_partitions=dynamic_partitions,
        incomplete=incomplete,
    )


def _mapping(environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is None:
        return dict(os.environ)
    return dict(environ)


def _as_path(value: str | Path) -> Path:
    return value if isinstance(value, Path) else Path(os.fspath(value))


def _clean_optional(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = os.fspath(value).strip().strip('"')
    return text or None


def _clean_target(target: str | None) -> str | None:
    if target is None:
        return None
    text = target.strip()
    return text or None


def _resolve_given(
    raw: str,
    *,
    target: str | None,
    cwd: Path,
    is_wsl: bool,
    environ: Mapping[str, str],
    source: str,
) -> ResolvedProductOut:
    path = normalize_user_path(raw, cwd=cwd, is_wsl=is_wsl, environ=dict(environ))
    is_link, link_target, broken = symlink_info(path)
    if broken:
        raise ProductOutError(
            f"PRODUCT_OUT is a broken symlink: {raw}"
            + (f" -> {link_target}" if link_target else "")
        )
    if not _exists(path):
        raise ProductOutError(f"PRODUCT_OUT does not exist: {raw}")
    if _is_file(path):
        raise ProductOutError(f"PRODUCT_OUT is a file, not a directory: {raw}")
    resolved = canonical_path(path)
    link2, tgt2, broken2 = symlink_info(resolved)
    candidates = _candidates_under(resolved, target=target)
    picked = _pick_unique(
        candidates,
        requested_path=raw,
        source=source,
        target=target,
    )
    return ResolvedProductOut(
        path=picked.path,
        requested_path=raw,
        source=source,
        is_symlink=is_link or link2 or picked.is_symlink,
        symlink_target=link_target or tgt2 or picked.symlink_target,
        broken_symlink=broken or broken2 or picked.broken_symlink,
    )


def _pick_unique(
    candidates: list[Path],
    *,
    requested_path: str,
    source: str,
    target: str | None,
) -> ResolvedProductOut:
    unique = _unique_paths(candidates)
    if not unique:
        hint = " Pass --product-out PATH or set PRODUCT_OUT."
        if target:
            hint = f" No directory named {target!r} was found." + hint
        raise ProductOutError(
            f"PRODUCT_OUT could not be resolved from {requested_path}." + hint
        )
    if len(unique) > 1:
        names = [str(path) for path in unique]
        raise AmbiguousProductOutError(
            "multiple PRODUCT_OUT candidates found; pass --product-out or --target "
            "instead of picking interactively: " + ", ".join(names),
            names,
        )
    chosen = unique[0]
    is_link, link_target, broken = symlink_info(chosen)
    if broken:
        raise ProductOutError(
            f"PRODUCT_OUT is a broken symlink: {chosen}"
            + (f" -> {link_target}" if link_target else "")
        )
    return ResolvedProductOut(
        path=canonical_path(chosen),
        requested_path=requested_path,
        source=source,
        is_symlink=is_link,
        symlink_target=link_target,
        broken_symlink=broken,
    )


def _candidates_under(path: Path, *, target: str | None) -> list[Path]:
    if _is_product_dir(path):
        if target is None or path.name == target:
            return [path]
        nested = path / target
        if _is_dir(nested) and (target is None or nested.name == target):
            return [nested]
        return []
    product_root = _find_product_root(path)
    if product_root is not None:
        return _child_products(product_root, target)
    if target is not None:
        nested = path / target
        if _is_product_dir(nested):
            return [nested]
    return []


def _discover_from_cwd(cwd: Path, *, target: str | None) -> list[Path]:
    current = canonical_path(cwd)
    for _ in range(_PARENT_WALK_LIMIT):
        if _is_product_dir(current):
            if target is None or current.name == target:
                return [current]
            return []
        product_root = current / "out" / "target" / "product"
        if _is_dir(product_root):
            return _child_products(product_root, target)
        if current.name == "product" and current.parent.name == "target" and _is_dir(current):
            return _child_products(current, target)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return []


def _list_under(path: Path) -> list[Path]:
    if _is_product_dir(path):
        return [canonical_path(path)]
    product_root = _find_product_root(path)
    if product_root is not None:
        return _child_products(product_root, None)
    if path.name == "product" and path.parent.name == "target" and _is_dir(path):
        return _child_products(path, None)
    nested = path / "out" / "target" / "product"
    if _is_dir(nested):
        return _child_products(nested, None)
    return []


def _find_product_root(path: Path) -> Path | None:
    direct = path / "out" / "target" / "product"
    if _is_dir(direct):
        return direct
    if path.name == "product" and path.parent.name == "target" and _is_dir(path):
        return path
    if path.name == "target" and _is_dir(path / "product"):
        return path / "product"
    if path.name == "out" and _is_dir(path / "target" / "product"):
        return path / "target" / "product"
    return None


def _child_products(product_root: Path, target: str | None) -> list[Path]:
    try:
        entries = sorted(product_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ProductOutError(f"cannot read {product_root}: {exc}") from exc
    found: list[Path] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if not _is_dir(entry):
            continue
        if target is not None and entry.name != target:
            continue
        found.append(canonical_path(entry))
    return found


def _is_product_dir(path: Path) -> bool:
    if not _is_dir(path):
        return False
    # AOSP tree roots contain out/target/product; they are not product dirs.
    if _is_dir(path / "out" / "target" / "product"):
        return False
    parent = path.parent
    if parent.name == "product" and parent.parent.name == "target":
        return True
    for marker in _PRODUCT_MARKERS:
        if _is_file(path / marker):
            return True
    try:
        if any(p for p in path.glob("build_fingerprint-*.txt") if _is_file(p)):
            return True
    except OSError:
        return False
    return _is_file(path / "system" / "build.prop")


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        canon = canonical_path(path)
        if canon in seen:
            continue
        seen.add(canon)
        unique.append(canon)
    return sorted(unique, key=lambda item: str(item))


def _coerce_resolved(
    product_out: str | Path | ResolvedProductOut,
    *,
    requested_path: str | None,
) -> ResolvedProductOut:
    if isinstance(product_out, ResolvedProductOut):
        if requested_path is None:
            return product_out
        return ResolvedProductOut(
            path=product_out.path,
            requested_path=requested_path,
            source=product_out.source,
            is_symlink=product_out.is_symlink,
            symlink_target=product_out.symlink_target,
            broken_symlink=product_out.broken_symlink,
        )
    raw = os.fspath(product_out)
    path = Path(raw)
    is_link, link_target, broken = symlink_info(path)
    if broken:
        raise ProductOutError(
            f"PRODUCT_OUT is a broken symlink: {raw}"
            + (f" -> {link_target}" if link_target else "")
        )
    if not _exists(path):
        raise ProductOutError(f"PRODUCT_OUT does not exist: {raw}")
    return ResolvedProductOut(
        path=canonical_path(path),
        requested_path=requested_path if requested_path is not None else raw,
        source="direct",
        is_symlink=is_link,
        symlink_target=link_target,
        broken_symlink=broken,
    )


def _inventory_kernels(
    directory: Path,
) -> tuple[Path | None, list[Artifact], list[Evidence]]:
    artifacts: list[Artifact] = []
    evidence: list[Evidence] = []
    selected: Path | None = None
    for name in KERNEL_CANDIDATES:
        path = directory / name
        if not _is_file(path):
            continue
        inspection = inspect_file(path)
        if selected is None:
            selected = path
            status = "detected"
            notes = [f"selected by priority ({name})"]
        else:
            status = "detected"
            notes = [f"not selected; {selected.name} has higher priority"]
        if inspection.architecture:
            evidence.append(
                Evidence("kernel", f"{name}.architecture", inspection.architecture)
            )
            notes = [*notes, f"inspect arch={inspection.architecture}"]
        artifacts.append(
            Artifact(
                name=name,
                kind="kernel",
                path=path,
                status=status,
                format=inspection.format,
                notes=notes + inspection.notes,
                inspection=inspection,
            )
        )
    return selected, artifacts, evidence


def _inventory_ramdisks(directory: Path) -> tuple[Path | None, list[Artifact]]:
    artifacts: list[Artifact] = []
    selected: Path | None = None
    for name in RAMDISK_CANDIDATES:
        path = directory / name
        if not _is_file(path):
            continue
        inspection = inspect_file(path)
        if selected is None:
            selected = path
            notes = [f"selected by priority ({name})"]
        else:
            notes = [f"not selected; {selected.name} has higher priority"]
        artifacts.append(
            Artifact(
                name=name,
                kind="ramdisk",
                path=path,
                status="detected",
                format=inspection.format,
                notes=notes + inspection.notes,
                inspection=inspection,
            )
        )
    return selected, artifacts


def _optional_image(
    directory: Path, name: str, kind: str, status: str
) -> tuple[Path | None, list[Artifact]]:
    path = directory / name
    if not _is_file(path):
        return None, []
    inspection = inspect_file(path)
    return path, [
        Artifact(
            name=name,
            kind=kind,
            path=path,
            status=status,  # type: ignore[arg-type]
            format=inspection.format,
            notes=list(inspection.notes),
            inspection=inspection,
        )
    ]


def _special_unsupported(
    directory: Path, name: str, kind: str, reason: str
) -> tuple[Path | None, list[Artifact]]:
    path = directory / name
    if not _is_file(path):
        return None, []
    inspection = inspect_file(path)
    return path, [
        Artifact(
            name=name,
            kind=kind,
            path=path,
            status="unsupported",
            format=inspection.format,
            notes=[reason, *inspection.notes],
            inspection=inspection,
        )
    ]


def _extra_root_images(directory: Path, already: set[str]) -> list[Artifact]:
    extras: list[Artifact] = []
    try:
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError:
        return extras
    skip = already | {
        "super_empty.img",
        "cache.img",
        "metadata.img",
    }
    for entry in entries:
        name = entry.name
        if name in skip or name.endswith(".lock"):
            continue
        if not name.endswith(".img") or not _is_file(entry):
            continue
        if name.startswith("vendor_boot") or name.startswith("super"):
            inspection = inspect_file(entry)
            extras.append(
                Artifact(
                    name=name,
                    kind=name.removesuffix(".img"),
                    path=entry,
                    status="unsupported",
                    format=inspection.format,
                    notes=[
                        "variant of vendor_boot/super; never treated as a regular disk",
                        *inspection.notes,
                    ],
                    inspection=inspection,
                )
            )
            continue
        if name.startswith("vbmeta"):
            inspection = inspect_file(entry)
            extras.append(
                Artifact(
                    name=name,
                    kind="vbmeta",
                    path=entry,
                    status="optional",
                    format=inspection.format,
                    notes=list(inspection.notes),
                    inspection=inspection,
                )
            )
            continue
        inspection = inspect_file(entry)
        extras.append(
            Artifact(
                name=name,
                kind=name.removesuffix(".img"),
                path=entry,
                status="optional",
                format=inspection.format,
                notes=list(inspection.notes),
                inspection=inspection,
            )
        )
    return extras


def _load_properties(directory: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    for relative in _BUILD_PROP_RELATIVE:
        path = directory / relative
        if not _is_file(path):
            continue
        for key, value in _parse_key_values(_read_text(path) or "").items():
            props[key] = value
    misc = directory / "misc_info.txt"
    if _is_file(misc):
        for key, value in _parse_key_values(_read_text(misc) or "").items():
            props.setdefault(key, value)
    return props


def _load_required_images(directory: Path) -> set[str]:
    path = directory / "required_images"
    text = _read_text(path)
    if text is None:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")}


def _load_key_values(path: Path) -> dict[str, str]:
    text = _read_text(path)
    if text is None:
        return {}
    return _parse_key_values(text)


def _load_fingerprints(directory: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    try:
        matches = sorted(directory.glob("build_fingerprint-*.txt"))
    except OSError:
        return found
    for path in matches:
        if not _is_file(path):
            continue
        found.append((path, (_read_text(path) or "").strip()))
    return found


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("import "):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def _collect_prop_arch_evidence(evidence: list[Evidence], properties: dict[str, str]) -> None:
    for key in _ABI_PRIMARY_KEYS:
        if key in properties:
            evidence.append(Evidence("build.prop", key, properties[key]))
    for key in _ABI_LIST_KEYS:
        if key not in properties:
            continue
        raw = properties[key]
        primary = raw.split(",", 1)[0].strip()
        if primary:
            evidence.append(Evidence("build.prop", f"{key}.primary", primary))
        extras = [item.strip() for item in raw.split(",")[1:] if item.strip()]
        if extras:
            evidence.append(
                Evidence("build.prop", f"{key}.translated", ",".join(extras))
            )
    for key in _HARDWARE_KEYS + _MODEL_KEYS + _BOARD_KEYS + _NAME_KEYS:
        if key in properties:
            evidence.append(Evidence("build.prop", key, properties[key]))


def _collect_fingerprint_arch_evidence(
    evidence: list[Evidence], fingerprints: list[tuple[Path, str]]
) -> None:
    for path, text in fingerprints:
        evidence.append(Evidence("build_fingerprint", path.name, text or path.stem))
        _record_arch_token(evidence, "build_fingerprint", path.name, path.name)
        if text:
            _record_arch_token(evidence, "build_fingerprint", "content", text)


def _collect_family_name_evidence(
    evidence: list[Evidence],
    target_name: str,
    properties: dict[str, str],
    fingerprints: list[tuple[Path, str]],
    android_info: dict[str, str],
) -> None:
    lowered = target_name.lower()
    if "vsoc_" in lowered or lowered.startswith("vsoc"):
        evidence.append(Evidence("directory_name", "vsoc_", target_name))
    if "emu64x" in lowered:
        evidence.append(Evidence("directory_name", "emu64x", target_name))

    for key in _NAME_KEYS:
        value = properties.get(key, "")
        if "aosp_cf_" in value.lower():
            evidence.append(Evidence("build.prop", key, value))
    for key in _MODEL_KEYS:
        value = properties.get(key, "")
        if "cuttlefish" in value.lower():
            evidence.append(Evidence("build.prop", key, value))
    for key in _BOARD_KEYS:
        value = properties.get(key, "")
        if "cutf" in value.lower():
            evidence.append(Evidence("build.prop", key, value))
    for key in _HARDWARE_KEYS:
        value = properties.get(key, "")
        hardware = value.lower()
        if hardware in {"ranchu", "goldfish"} or hardware.startswith(("ranchu", "goldfish")):
            evidence.append(Evidence("build.prop", key, value))

    for path, text in fingerprints:
        blob = f"{path.name} {text}".lower()
        if "aosp_cf_" in blob:
            evidence.append(Evidence("build_fingerprint", path.name, text or path.name))
        if "sdk_phone" in blob:
            evidence.append(Evidence("build_fingerprint", path.name, text or path.name))
        if "cuttlefish" in blob:
            evidence.append(Evidence("build_fingerprint", path.name, text or path.name))

    board = android_info.get("board", "")
    if "cutf" in board.lower():
        evidence.append(Evidence("android-info.txt", "board", board))
    for key, value in android_info.items():
        if "cuttlefish" in f"{key}={value}".lower():
            evidence.append(Evidence("android-info.txt", key, value))


def _record_arch_token(evidence: list[Evidence], source: str, key: str, text: str) -> None:
    token = _arch_from_text(text)
    if token:
        evidence.append(Evidence(source, f"{key}.arch", token))


def _arch_from_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    lowered = stripped.lower().replace("-", "_")
    if lowered in {"x86_64", "x86-64", "amd64", "emu64x"}:
        return "x86_64"
    if lowered in {"arm64-v8a", "arm64", "aarch64"}:
        return "arm64"
    if lowered in {"armeabi-v7a", "armeabi", "armv7", "arm"}:
        return "arm"
    if lowered == "x86":
        return "x86"
    if _X86_64_RE.search(stripped):
        return "x86_64"
    if _ARM64_RE.search(stripped):
        return "arm64"
    if _ARM32_RE.search(stripped):
        return "arm"
    if lowered.startswith("x86_") and "64" not in lowered:
        return "x86"
    if _X86_32_RE.search(stripped):
        return "x86"
    return None


def _arch_family(arch: str) -> str | None:
    if arch in {"x86_64", "x86"}:
        return "x86"
    if arch in {"arm64", "arm"}:
        return "arm"
    return None


def _conclude_architecture(evidence: list[Evidence]) -> str:
    """Return x86_64 or raise. Extra ABIs in abilist are not guest-arch conflicts."""

    guest: list[tuple[Evidence, str]] = []
    for item in evidence:
        if item.key.endswith(".translated"):
            continue
        token = _arch_from_text(item.value)
        if token is None:
            continue
        if item.key.endswith(".arch"):
            guest.append((item, token))
            continue
        if item.source in {"kernel", "directory_name", "build_fingerprint"}:
            guest.append((item, token))
            continue
        if item.source == "build.prop" and (
            item.key in _ABI_PRIMARY_KEYS
            or item.key.endswith(".primary")
            or item.key in _NAME_KEYS
            or item.key in _MODEL_KEYS
        ):
            guest.append((item, token))
    families = {_arch_family(token) for _, token in guest if _arch_family(token)}
    families.discard(None)
    detail = "; ".join(f"{item.source}:{item.key}={item.value}" for item, _ in guest) or "no evidence"
    if "x86" in families and "arm" in families:
        raise ArchitectureError(
            "conflicting architecture evidence (x86_64 vs ARM); refusing to guess: " + detail
        )
    tokens = {token for _, token in guest}
    if "x86_64" in tokens:
        return "x86_64"
    if tokens & {"arm64", "arm"}:
        raise ArchitectureError(f"guest architecture is not x86_64: {detail}")
    if "x86" in tokens:
        raise ArchitectureError(f"guest architecture is x86, not x86_64: {detail}")
    raise ArchitectureError(f"guest architecture is missing, not x86_64: {detail}")


def _classify_family(evidence: list[Evidence]) -> tuple[str, str, list[ValidationIssue]]:
    ranchu = [item for item in evidence if _is_ranchu_item(item)]
    cuttlefish = [item for item in evidence if _is_cuttlefish_item(item)]
    issues: list[ValidationIssue] = []
    if ranchu and cuttlefish:
        issues.append(
            ValidationIssue(
                severity="error",
                code="conflicting_family",
                message=(
                    "product family evidence conflicts (emulator_ranchu vs cuttlefish); "
                    "not guessing"
                ),
            )
        )
        return "conflicting", "conflict", issues
    if cuttlefish:
        confidence = "high" if len(cuttlefish) >= 2 else "medium"
        return "cuttlefish", confidence, issues
    if ranchu:
        confidence = "high" if len(ranchu) >= 2 else "medium"
        return "emulator_ranchu", confidence, issues
    return "unknown", "none", issues


def _is_ranchu_item(item: Evidence) -> bool:
    blob = f"{item.key} {item.value}".lower()
    if item.key in {"kernel-ranchu", "kernel-ranchu-64"} or item.key.startswith("kernel-ranchu"):
        return True
    if item.key == "advancedFeatures.ini":
        return True
    if item.key == "emu64x" or item.value.lower() == "emu64x":
        return True
    if "sdk_phone" in blob:
        return True
    if item.key in _HARDWARE_KEYS and any(name in item.value.lower() for name in ("ranchu", "goldfish")):
        return True
    return False


def _is_cuttlefish_item(item: Evidence) -> bool:
    blob = f"{item.key} {item.value}".lower()
    if item.key in {"vsoc_", "super.img+vendor_boot.img"}:
        return True
    if "vsoc_" in blob:
        return True
    if "aosp_cf_" in blob:
        return True
    if "cuttlefish" in blob:
        return True
    if "cutf" in blob and item.key in {"ro.product.board", "ro.product.vendor.board", "board", *_BOARD_KEYS}:
        return True
    if item.key in _BOARD_KEYS and "cutf" in item.value.lower():
        return True
    return False


def _has_ranchu_evidence(evidence: list[Evidence]) -> bool:
    return any(_is_ranchu_item(item) for item in evidence)


def _mark_ranchu_completeness(
    artifacts: list[Artifact],
    issues: list[ValidationIssue],
    *,
    kernel: Path | None,
    ramdisk: Path | None,
    system: Path | None,
    directory: Path,
) -> bool:
    missing: list[str] = []
    if kernel is None:
        missing.append("kernel")
        if all(item.kind != "kernel" for item in artifacts):
            artifacts.append(
                Artifact(name=KERNEL_CANDIDATES[0], kind="kernel", path=None, status="missing")
            )
        issues.append(
            ValidationIssue(
                severity="error",
                code="missing_kernel",
                message="ranchu product is missing a kernel (kernel-ranchu-64/kernel-ranchu/kernel/bzImage/Image)",
                path=str(directory),
            )
        )
    if ramdisk is None:
        missing.append("ramdisk")
        if all(item.kind != "ramdisk" for item in artifacts):
            artifacts.append(
                Artifact(name=RAMDISK_CANDIDATES[0], kind="ramdisk", path=None, status="missing")
            )
        issues.append(
            ValidationIssue(
                severity="error",
                code="missing_ramdisk",
                message="ranchu product is missing a ramdisk (ramdisk-qemu.img/ramdisk.img/initramfs.img)",
                path=str(directory),
            )
        )
    if system is None:
        missing.append("system")
        if all(item.kind != "system" for item in artifacts):
            artifacts.append(
                Artifact(name="system.img", kind="system", path=None, status="missing")
            )
        issues.append(
            ValidationIssue(
                severity="error",
                code="missing_system",
                message="ranchu product is missing system.img",
                path=str(directory),
            )
        )
    if missing:
        issues.append(
            ValidationIssue(
                severity="error",
                code="incomplete_ranchu",
                message="incomplete ranchu PRODUCT_OUT; missing " + ", ".join(missing),
                path=str(directory),
            )
        )
        return True
    return False


def _promote_status(artifacts: list[Artifact], kind: str, status: str) -> None:
    for item in artifacts:
        if item.kind == kind and item.status not in {"unsupported", "conflicting", "missing"}:
            item.status = status  # type: ignore[assignment]
            return
        if item.kind == kind and status == "missing":
            item.status = "missing"
            return


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False
