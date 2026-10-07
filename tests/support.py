"""Helpers for tiny synthetic PRODUCT_OUT directories.

These fixtures contain magic bytes and text metadata only. They are
**not bootable** Android images and must never be treated as such.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_src_str = str(_SRC)
if _src_str not in sys.path:
    sys.path.insert(0, _src_str)

# Android sparse magic 0xED26FF3A little-endian → bytes 3A FF 26 ED
SPARSE_MAGIC = 0xED26FF3A
LP_GEOMETRY_MAGIC = b"gDla"
BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"
ELF_MAGIC = b"\x7fELF"
EM_X86_64 = 62
EM_AARCH64 = 183


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sparse_header(*, extra: bytes = b"") -> bytes:
    """Minimal valid-looking Android sparse header (not a bootable image)."""

    header = struct.pack(
        "<IHHHHIIII",
        SPARSE_MAGIC,
        1,  # major
        0,  # minor
        28,  # file header size
        12,  # chunk header size
        4096,  # block size
        1,  # total blocks
        1,  # total chunks
        0,  # checksum
    )
    return header + extra


def write_sparse(path: Path) -> Path:
    return write_bytes(path, sparse_header())


def write_sparse_super(path: Path) -> Path:
    """Sparse image with LP geometry magic in the header window (not raw)."""

    return write_bytes(path, sparse_header(extra=b"\x00" * 4 + LP_GEOMETRY_MAGIC))


def elf_header(machine: int, *, ei_class: int = 2, ei_data: int = 1) -> bytes:
    header = bytearray(64)
    header[0:4] = ELF_MAGIC
    header[4] = ei_class
    header[5] = ei_data
    header[6] = 1
    struct.pack_into("<H", header, 16, 2)  # ET_EXEC
    struct.pack_into("<H", header, 18, machine)
    return bytes(header)


def write_elf(path: Path, arch: str) -> Path:
    mapping = {"x86_64": EM_X86_64, "arm64": EM_AARCH64, "aarch64": EM_AARCH64}
    if arch not in mapping:
        raise ValueError(f"unsupported synthetic ELF arch: {arch}")
    return write_bytes(path, elf_header(mapping[arch]))


def bzimage_header(*, x64: bool = True) -> bytes:
    header = bytearray(0x240)
    header[0x1FE:0x200] = b"\x55\xaa"
    header[0x202:0x206] = b"HdrS"
    if x64:
        header[0x236] = 0x01  # XLF_KERNEL_64
    return bytes(header)


def write_bzimage(path: Path, *, x64: bool = True) -> Path:
    return write_bytes(path, bzimage_header(x64=x64))


def write_boot_img(path: Path) -> Path:
    return write_bytes(path, BOOT_MAGIC + b"\x00" * 16)


def write_vendor_boot(path: Path) -> Path:
    return write_bytes(path, VENDOR_BOOT_MAGIC + b"\x00" * 16)


def make_ranchu_product(root: Path, name: str = "emu64x") -> Path:
    """Synthetic ranchu/goldfish PRODUCT_OUT (magic bytes only, not bootable)."""

    product = root / name
    product.mkdir(parents=True, exist_ok=True)
    write_elf(product / "kernel-ranchu-64", "x86_64")
    write_bytes(product / "ramdisk-qemu.img", b"\x1f\x8b" + b"\x00" * 8)
    write_sparse(product / "system.img")
    write_sparse(product / "vendor.img")
    write_bytes(product / "userdata.img", b"\x00" * 64)
    write_text(product / "advancedFeatures.ini", "GLESDynamicVersion = on\n")
    write_text(product / "android-info.txt", "board=goldfish_x86_64\n")
    write_text(
        product / "build.prop",
        "\n".join(
            [
                "ro.product.cpu.abi=x86_64",
                "ro.product.cpu.abilist=x86_64,x86",
                "ro.hardware=ranchu",
                "ro.product.name=sdk_phone_x86_64",
                "ro.product.model=sdk_phone64_x86_64",
                "",
            ]
        ),
    )
    return product


def make_incomplete_ranchu(root: Path, name: str = "emu64x") -> Path:
    """Ranchu family with kernel only — missing ramdisk and system.img."""

    product = root / name
    product.mkdir(parents=True, exist_ok=True)
    write_elf(product / "kernel-ranchu", "x86_64")
    write_text(product / "advancedFeatures.ini", "GLESDynamicVersion = on\n")
    write_text(product / "build.prop", "ro.product.cpu.abi=x86_64\nro.hardware=ranchu\n")
    return product


def make_cuttlefish_product(root: Path, name: str = "vsoc_x86_64") -> Path:
    """Synthetic Cuttlefish PRODUCT_OUT (magic bytes only, not bootable)."""

    product = root / name
    product.mkdir(parents=True, exist_ok=True)
    write_vendor_boot(product / "vendor_boot.img")
    write_sparse_super(product / "super.img")
    write_boot_img(product / "boot.img")
    write_boot_img(product / "init_boot.img")
    write_sparse(product / "userdata.img")
    write_text(product / "required_images", "super.img\nvendor_boot.img\nuserdata.img\n")
    write_text(product / "android-info.txt", "board=cutf_cvm\n")
    write_text(product / "misc_info.txt", "use_dynamic_partitions=true\n")
    write_text(
        product / "system" / "build.prop",
        "\n".join(
            [
                "ro.product.cpu.abi=x86_64",
                "ro.product.name=aosp_cf_x86_64_phone",
                "ro.product.model=Cuttlefish x86_64 phone",
                "ro.product.board=cutf",
                "ro.hardware=cutf_cvm",
                "",
            ]
        ),
    )
    return product


def make_arm64_product(root: Path, name: str = "generic_arm64") -> Path:
    """Synthetic ARM64 product used to verify architecture refusal."""

    product = root / name
    product.mkdir(parents=True, exist_ok=True)
    write_elf(product / "kernel", "arm64")
    write_text(product / "build.prop", "ro.product.cpu.abi=arm64-v8a\n")
    return product


def make_aosp_tree(root: Path, targets: list[str] | None = None) -> Path:
    """Create ``out/target/product/<target>`` under ``root``."""

    product_root = root / "out" / "target" / "product"
    product_root.mkdir(parents=True, exist_ok=True)
    names = targets if targets is not None else ["emu64x", "vsoc_x86_64"]
    for name in names:
        if "vsoc" in name or name.startswith("aosp_cf"):
            make_cuttlefish_product(product_root, name)
        else:
            make_ranchu_product(product_root, name)
    return root
