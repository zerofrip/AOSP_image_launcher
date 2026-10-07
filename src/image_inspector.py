"""Inspect Android/AOSP image and kernel files without modifying them."""

from __future__ import annotations

import struct
from pathlib import Path

from models import ImageInspection
from paths import symlink_info

# Android sparse image magic (little-endian 0xED26FF3A) → bytes 3A FF 26 ED
SPARSE_MAGIC = 0xED26FF3A
BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"
ELF_MAGIC = b"\x7fELF"
LP_GEOMETRY_MAGIC = b"gDla"
GZIP_MAGIC = b"\x1f\x8b"
LZ4_MAGIC = b"\x04\x22\x4d\x18"
CPIO_NEWC = b"070701"

EM_386 = 3
EM_X86_64 = 62
EM_ARM = 40
EM_AARCH64 = 183
EM_RISCV = 243

_HEADER_READ = 4096


def inspect_file(path: Path) -> ImageInspection:
    is_link, target, broken = symlink_info(path)
    notes: list[str] = []
    if is_link:
        notes.append(f"symlink -> {target}")
    if broken:
        return ImageInspection(
            path=path,
            size=0,
            format="missing",
            notes=notes + ["broken symlink"],
            is_symlink=is_link,
            symlink_target=target,
            broken_symlink=True,
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        return ImageInspection(
            path=path,
            size=0,
            format="unreadable",
            notes=[str(exc)],
            is_symlink=is_link,
            symlink_target=target,
            broken_symlink=False,
        )
    header = _read_prefix(path, max(_HEADER_READ, 0x240 + 16))
    fmt, arch, extra = classify_bytes(header, size)
    notes.extend(extra)
    if size == 0:
        notes.append("empty file")
    return ImageInspection(
        path=path,
        size=size,
        format=fmt,
        architecture=arch,
        notes=notes,
        is_symlink=is_link,
        symlink_target=target,
        broken_symlink=False,
    )


def classify_bytes(header: bytes, size: int) -> tuple[str, str | None, list[str]]:
    notes: list[str] = []
    if len(header) >= 4 and struct.unpack_from("<I", header, 0)[0] == SPARSE_MAGIC:
        notes.extend(_sparse_notes(header))
        if size >= 28 and LP_GEOMETRY_MAGIC in header:
            notes.append("LP geometry magic gDla found inside sparse header window")
            return "android_sparse_super", None, notes
        return "android_sparse", None, notes
    if header.startswith(VENDOR_BOOT_MAGIC):
        return "vendor_boot", None, notes
    if header.startswith(BOOT_MAGIC):
        return "boot_android", None, notes
    if header.startswith(ELF_MAGIC):
        arch, elf_notes = _elf_arch(header)
        notes.extend(elf_notes)
        return "elf", arch, notes
    bz_arch, bz_notes = _bzimage(header)
    if bz_arch or bz_notes:
        notes.extend(bz_notes)
        return "bzimage", bz_arch, notes
    if LP_GEOMETRY_MAGIC in header[:1024]:
        return "lp_metadata", None, notes
    if header.startswith(GZIP_MAGIC):
        return "gzip", None, notes
    if header.startswith(LZ4_MAGIC):
        return "lz4", None, notes
    if header.startswith(CPIO_NEWC) or CPIO_NEWC in header[:16]:
        return "cpio", None, notes
    if size > 0 and header[:16] == b"\x00" * min(16, len(header)):
        notes.append("leading zeros; not android sparse; raw/unknown")
        return "raw_or_unknown", None, notes
    return "unknown", None, notes


def is_android_sparse(path: Path) -> bool:
    header = _read_prefix(path, 4)
    return len(header) >= 4 and struct.unpack_from("<I", header, 0)[0] == SPARSE_MAGIC


def _read_prefix(path: Path, n: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(n)
    except OSError:
        return b""


def _sparse_notes(header: bytes) -> list[str]:
    notes = [f"android sparse magic 0x{SPARSE_MAGIC:08X} (LE bytes 3A FF 26 ED)"]
    if len(header) < 28:
        notes.append("sparse header truncated")
        return notes
    magic, major, minor, file_hdr, chunk_hdr, blk, total_blks, total_chunks, crc = struct.unpack_from(
        "<IHHHHIIII", header, 0
    )
    notes.append(
        f"sparse v{major}.{minor} hdr={file_hdr}/{chunk_hdr} blk={blk} "
        f"chunks={total_chunks} checksum={crc}"
    )
    return notes


def _elf_arch(header: bytes) -> tuple[str | None, list[str]]:
    notes: list[str] = []
    if len(header) < 20:
        return None, ["ELF header truncated"]
    ei_class = header[4]
    ei_data = header[5]
    endian = "<" if ei_data == 1 else ">"
    if ei_class == 2:
        notes.append("ELF64")
    elif ei_class == 1:
        notes.append("ELF32")
    machine = struct.unpack_from(endian + "H", header, 18)[0]
    mapping = {
        EM_X86_64: "x86_64",
        EM_386: "x86",
        EM_AARCH64: "arm64",
        EM_ARM: "arm",
        EM_RISCV: "riscv",
    }
    arch = mapping.get(machine)
    notes.append(f"e_machine={machine}" + (f" ({arch})" if arch else ""))
    return arch, notes


def _bzimage(header: bytes) -> tuple[str | None, list[str]]:
    if len(header) < 0x204:
        return None, []
    if header[0x202:0x206] != b"HdrS":
        return None, []
    notes = ["x86 Linux setup header HdrS"]
    if len(header) > 0x1FF and header[0x1FE:0x200] == b"\x55\xaa":
        notes.append("boot signature 0xAA55")
    arch = "x86"
    if len(header) > 0x236:
        xloadflags = header[0x236]
        if xloadflags & 0x01:
            arch = "x86_64"
            notes.append("XLF_KERNEL_64")
    return arch, notes
