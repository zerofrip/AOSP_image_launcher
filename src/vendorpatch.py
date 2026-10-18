"""Patch a real AOSP vendor.img with a minimal /misc fstab file.

After first-stage init's ``switch_root`` to ``/system``, second-stage init
and HAL services can only discover fstab files that live on the real
mounted vendor/system partitions. This module extracts an EROFS vendor
image, injects a tiny ``fstab.<hardware>`` containing only the safe,
universal ``/misc`` line the launcher already uses in its ramdisk fstab,
and rebuilds the image with correct SELinux labeling via AOSP's own
``build_image`` tool.

All failures return a warning string instead of raising, so the caller
can fall back to the existing ramdisk-only fstab behavior.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from capabilities import ToolInfo

EROFS_MAGIC = b"\xe2\xe1\xf5\xe0"
EROFS_MAGIC_OFFSET = 1024

_VENDOR_PROP_GLOB = (
    "out/soong/.intermediates/build/soong/fsgen/*_generated_vendor_image/"
    "android_common/prop"
)
_FILE_CONTEXTS_BIN = (
    "out/soong/.intermediates/system/sepolicy/file_contexts_bin_gen/"
    "android_common/gen/file_contexts.bin"
)


def _is_erofs_image(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < EROFS_MAGIC_OFFSET + len(EROFS_MAGIC):
        return False
    return data[EROFS_MAGIC_OFFSET:EROFS_MAGIC_OFFSET + len(EROFS_MAGIC)] == EROFS_MAGIC


def _resolve_prop_value(key: str, value: str, aosp_root: Path) -> str:
    """Join AOSP-root-relative filesystem paths in prop values with aosp_root.

    Paths in the real generated vendor prop file (e.g. selinux_fc,
    erofs_default_compress_hints) are relative to the AOSP source root,
    not to the working directory build_image is invoked from. Absolute
    values and non-path values (e.g. mount_point) pass through unchanged.
    """
    if not value or Path(value).is_absolute():
        return value
    if key.endswith("_fc") or "compress_hints" in key:
        return str(aosp_root / value)
    return value


def _build_properties_file(
    aosp_root: Path,
    product_out: Path,
    tmpdir: Path,
) -> Path:
    aosp_root = aosp_root.resolve()
    prop_path = tmpdir / "vendor.prop"
    glob_base = aosp_root / "out/soong/.intermediates/build/soong/fsgen"
    matches = sorted(glob_base.glob("*_generated_vendor_image/android_common/prop"))
    if len(matches) == 1:
        text = matches[0].read_text(encoding="utf-8", errors="replace")
        lines = []
        for line in text.splitlines():
            if line.startswith("avb_"):
                continue
            key, sep, value = line.partition("=")
            if sep:
                lines.append(f"{key}={_resolve_prop_value(key, value, aosp_root)}")
            else:
                lines.append(line)
        prop_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        file_contexts = aosp_root / _FILE_CONTEXTS_BIN
        lines = [
            "fs_type=erofs",
            "mount_point=vendor",
            "partition_name=vendor",
            "disable_sparse=true",
            f"selinux_fc={file_contexts}",
        ]
        prop_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return prop_path


def patch_vendor_image(
    vendor_image: Path,
    fstab_filename: str,
    misc_device: str,
    aosp_root: Path | None,
    product_out: Path | None,
    fsck_erofs: ToolInfo,
    mkfs_erofs: ToolInfo,
    build_image: ToolInfo,
) -> tuple[Path | None, str | None]:
    """Extract, patch, and rebuild an EROFS vendor image.

    Returns ``(patched_image_path, None)`` on success, or
    ``(None, warning_string)`` on any failure. The original ``vendor_image``
    file is never modified; only the returned temporary copy is used.
    """
    if not fsck_erofs.available or fsck_erofs.path is None:
        return None, "fsck.erofs not available; vendor.img will not be patched with /misc fstab"
    if not mkfs_erofs.available or mkfs_erofs.path is None:
        return None, "mkfs.erofs not available; vendor.img will not be patched with /misc fstab"
    if not build_image.available or build_image.path is None:
        return None, "build_image not available; vendor.img will not be patched with /misc fstab"
    if aosp_root is None:
        return None, "AOSP root not found; vendor.img will not be patched with /misc fstab"
    if product_out is None:
        return None, "PRODUCT_OUT not known; vendor.img will not be patched with /misc fstab"
    if not _is_erofs_image(vendor_image):
        return (
            None,
            f"{vendor_image} is not an EROFS image; vendor.img will not be patched with /misc fstab",
        )

    workdir = Path(tempfile.mkdtemp())
    try:
        extract_dir = workdir / "extract"
        extract_dir.mkdir()

        result = subprocess.run(
            [str(fsck_erofs.path), f"--extract={extract_dir}", str(vendor_image)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else "(no stderr)"
            return None, f"fsck.erofs extraction failed: {stderr}"

        fstab_dir = extract_dir / "etc"
        fstab_dir.mkdir(parents=True, exist_ok=True)
        fstab_path = fstab_dir / fstab_filename
        fstab_path.write_text(f"{misc_device} /misc emmc defaults defaults\n", encoding="utf-8")

        # Inject graphics HAL property overrides so ANGLE + SwiftShader are
        # selected when the build ships them but does not already set these
        # properties. Never duplicate or override an existing key.
        build_prop_path = extract_dir / "build.prop"
        build_prop_text = ""
        if build_prop_path.is_file():
            try:
                build_prop_text = build_prop_path.read_text(encoding="utf-8")
            except OSError:
                build_prop_text = ""
        present_keys = set()
        for line in build_prop_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                present_keys.add(line.split("=", 1)[0].strip())
        additions = [
            f"{key}={value}"
            for key, value in (
                ("ro.hardware.egl", "angle"),
                ("ro.hardware.vulkan", "pastel"),
            )
            if key not in present_keys
        ]
        if additions:
            try:
                with build_prop_path.open("a", encoding="utf-8") as bp:
                    if build_prop_text and not build_prop_text.endswith("\n"):
                        bp.write("\n")
                    bp.write("\n".join(additions) + "\n")
            except OSError as exc:
                return None, f"could not patch vendor.img build.prop: {exc}"

        prop_path = _build_properties_file(aosp_root, product_out, workdir)

        output_path = workdir / "vendor.img"
        result = subprocess.run(
            [
                str(build_image.path),
                str(extract_dir),
                str(prop_path),
                str(output_path),
                str(product_out),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else "(no stderr)"
            return None, f"build_image failed: {stderr}"

        return output_path, None
    except OSError as exc:
        return None, f"could not patch vendor.img: {exc}"
