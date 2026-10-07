"""Native, stdlib-only parsing of Android boot.img / init_boot.img /
vendor_boot.img, and combining their sections into a single kernel +
initrd + cmdline suitable for a direct ``qemu-system-x86_64 -kernel/-initrd``
boot.

No third-party dependencies and no external AOSP host tools (unpack_bootimg)
are required. Formats and the in-memory combining order (vendor ramdisk(s)
then generic ramdisk then bootconfig trailer) are verified against real
AOSP build artifacts and against the reference Kotlin implementation in
https://github.com/cfig/Android_boot_image_editor
(bbootimg/src/main/kotlin/bootimg/v3/{BootHeaderV3,VendorBootHeader,
VendorBoot,BootV3}.kt and doc/layout.md section 5 "boot in memory").
"""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"

_V3V4_HEADER_VERSION_OFFSET = 40
_V3_HEADER_SIZE = 1580
_V4_HEADER_SIZE = 1584
_V3V4_PAGE_SIZE = 4096

_VENDOR_HEADER_VERSION_OFFSET = 8
_VENDOR_V3_HEADER_SIZE = 2112
_VENDOR_V4_HEADER_SIZE = 2128


class BootImageError(Exception):
    """Raised for an unreadable, truncated, or unsupported boot/vendor_boot image."""


def _round_up(value: int, align: int) -> int:
    return ((value + align - 1) // align) * align


@dataclass
class BootSections:
    """Parsed boot.img / init_boot.img (same on-disk layout)."""

    header_version: int
    page_size: int
    kernel: bytes
    ramdisk: bytes
    cmdline: str


@dataclass
class VendorBootSections:
    """Parsed vendor_boot.img (header v3 or v4)."""

    header_version: int
    page_size: int
    ramdisk: bytes
    bootconfig: bytes
    cmdline: str


def _require(data: bytes, size: int, what: str) -> None:
    if len(data) < size:
        raise BootImageError(f"truncated {what}: need at least {size} bytes, got {len(data)}")


def read_boot_image(path: Path) -> BootSections:
    """Parse boot.img or init_boot.img (header v0-v2 legacy, or v3/v4 GKI)."""

    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise BootImageError(f"cannot read {path}: {exc}") from exc

    _require(data, 8, "boot image magic")
    if data[0:8] != BOOT_MAGIC:
        raise BootImageError(f"{path}: not an Android boot image (bad magic)")

    _require(data, _V3V4_HEADER_VERSION_OFFSET + 4, "boot image header_version field")
    header_version = struct.unpack_from("<I", data, _V3V4_HEADER_VERSION_OFFSET)[0]

    if header_version in (3, 4):
        min_size = _V4_HEADER_SIZE if header_version == 4 else _V3_HEADER_SIZE
        _require(data, min_size, f"boot image v{header_version} header")
        kernel_size = struct.unpack_from("<I", data, 8)[0]
        ramdisk_size = struct.unpack_from("<I", data, 12)[0]
        cmdline_raw = data[44:44 + 1536]
        cmdline = cmdline_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
        page_size = _V3V4_PAGE_SIZE

        kernel_offset = page_size
        kernel_end = kernel_offset + kernel_size
        ramdisk_offset = _round_up(kernel_end, page_size)
        ramdisk_end = ramdisk_offset + ramdisk_size

        _require(data, kernel_end, "boot image kernel section")
        _require(data, ramdisk_end, "boot image ramdisk section")

        kernel = data[kernel_offset:kernel_end]
        ramdisk = data[ramdisk_offset:ramdisk_end]
        return BootSections(
            header_version=header_version,
            page_size=page_size,
            kernel=kernel,
            ramdisk=ramdisk,
            cmdline=cmdline,
        )

    if header_version in (0, 1, 2):
        # Legacy header: page_size is NOT fixed, read it from the file.
        _require(data, 40, "legacy boot image header (page_size field)")
        kernel_size = struct.unpack_from("<I", data, 8)[0]
        ramdisk_size = struct.unpack_from("<I", data, 16)[0]
        page_size = struct.unpack_from("<I", data, 36)[0]
        if page_size <= 0:
            raise BootImageError(f"{path}: invalid page_size {page_size}")

        _require(data, 64 + 512, "legacy boot image cmdline (part 1)")
        cmdline1 = data[64:64 + 512].split(b"\x00", 1)[0]
        cmdline2 = b""
        if len(data) >= 608 + 1024:
            cmdline2 = data[608:608 + 1024].split(b"\x00", 1)[0]
        cmdline = (cmdline1 + cmdline2).decode("utf-8", errors="replace").strip()

        # Headers of this family are always smaller than one page in practice;
        # the kernel always starts at the first page boundary regardless of
        # the exact header_size reported (this matches both the v0 and v1/v2
        # on-disk layouts).
        kernel_offset = page_size
        kernel_end = kernel_offset + kernel_size
        ramdisk_offset = _round_up(kernel_end, page_size)
        ramdisk_end = ramdisk_offset + ramdisk_size

        _require(data, kernel_end, "legacy boot image kernel section")
        _require(data, ramdisk_end, "legacy boot image ramdisk section")

        kernel = data[kernel_offset:kernel_end]
        ramdisk = data[ramdisk_offset:ramdisk_end]
        return BootSections(
            header_version=header_version,
            page_size=page_size,
            kernel=kernel,
            ramdisk=ramdisk,
            cmdline=cmdline,
        )

    raise BootImageError(f"{path}: unsupported boot image header_version {header_version}")


def read_vendor_boot_image(path: Path) -> VendorBootSections:
    """Parse vendor_boot.img (header v3 or v4 only)."""

    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise BootImageError(f"cannot read {path}: {exc}") from exc

    _require(data, 8, "vendor_boot image magic")
    if data[0:8] != VENDOR_BOOT_MAGIC:
        raise BootImageError(f"{path}: not an Android vendor_boot image (bad magic)")

    _require(data, _VENDOR_HEADER_VERSION_OFFSET + 4, "vendor_boot header_version field")
    header_version = struct.unpack_from("<I", data, _VENDOR_HEADER_VERSION_OFFSET)[0]
    if header_version not in (3, 4):
        raise BootImageError(f"{path}: unsupported vendor_boot header_version {header_version}")

    min_size = _VENDOR_V4_HEADER_SIZE if header_version == 4 else _VENDOR_V3_HEADER_SIZE
    _require(data, min_size, f"vendor_boot v{header_version} header")

    page_size = struct.unpack_from("<I", data, 12)[0]
    if page_size <= 0:
        raise BootImageError(f"{path}: invalid page_size {page_size}")
    vnd_ramdisk_total_size = struct.unpack_from("<I", data, 24)[0]
    cmdline_raw = data[28:28 + 2048]
    cmdline = cmdline_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
    dtb_size = struct.unpack_from("<I", data, 2100)[0]

    bootconfig_size = 0
    vrt_size = 0
    if header_version == 4:
        vrt_size = struct.unpack_from("<I", data, 2112)[0]
        bootconfig_size = struct.unpack_from("<I", data, 2124)[0]

    # Ramdisk position: always round up the V3 header-size constant (2112),
    # even for v4 — this matches the reference implementation exactly and is
    # numerically equivalent to rounding up 2128 for any page_size that is a
    # power of two >= 16.
    ramdisk_offset = _round_up(_VENDOR_V3_HEADER_SIZE, page_size)
    ramdisk_end = ramdisk_offset + vnd_ramdisk_total_size
    _require(data, ramdisk_end, "vendor_boot ramdisk section")
    ramdisk = data[ramdisk_offset:ramdisk_end]

    dtb_offset = _round_up(ramdisk_end, page_size)
    dtb_end = dtb_offset + dtb_size

    bootconfig = b""
    if bootconfig_size > 0:
        vrt_offset = _round_up(dtb_end, page_size)
        vrt_end = vrt_offset + vrt_size
        bootconfig_offset = _round_up(vrt_end, page_size)
        bootconfig_end = bootconfig_offset + bootconfig_size
        _require(data, bootconfig_end, "vendor_boot bootconfig section")
        bootconfig = data[bootconfig_offset:bootconfig_end]

    return VendorBootSections(
        header_version=header_version,
        page_size=page_size,
        ramdisk=ramdisk,
        bootconfig=bootconfig,
        cmdline=cmdline,
    )


_BOOTCONFIG_MAGIC = b"#BOOTCONFIG\n"


def _finish_bootconfig_trailer(params: bytes) -> bytes:
    """Append the size+checksum+magic trailer the kernel's bootconfig
    parser expects to find at the very end of the initrd.

    vendor_boot.img's ``bootconfig`` section only stores the raw parameter
    text (verified against a real image: a 117-byte section matching
    exactly the parameter text length, with no trailer) — the trailer
    itself is assembled by whoever builds the final combined boot ramdisk
    (normally the bootloader; here, us). Checksum is the simple additive
    byte-sum matching the Linux kernel's lib/bootconfig.c
    ``xbc_calc_checksum()`` / tools/bootconfig's trailer format (NOT CRC32):
    sum of all parameter bytes, masked to 32 bits, little-endian, followed
    by the 4-byte little-endian size and the literal 12-byte magic.
    """

    checksum = sum(params) & 0xFFFFFFFF
    size = len(params)
    trailer = struct.pack("<II", size, checksum) + _BOOTCONFIG_MAGIC
    return params + trailer


_LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"


def _looks_like_legacy_lz4(data: bytes) -> bool:
    return data[:4] == _LZ4_LEGACY_MAGIC


def _lz4_decompress(data: bytes) -> bytes:
    """Decompress legacy-framed LZ4 data via the external ``lz4`` CLI tool.

    No pure-Python legacy-LZ4-frame decoder is implemented, so this
    requires ``lz4`` on PATH. Raises BootImageError if it is unavailable
    or decompression fails — callers should treat that as "best-effort
    enhancement unavailable", not a fatal error for the overall boot.img
    parse.
    """
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.lz4"
        dst = Path(td) / "out"
        src.write_bytes(data)
        try:
            subprocess.run(
                ["lz4", "-d", "-f", str(src), str(dst)],
                check=True, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BootImageError(f"lz4 decompress failed: {exc}") from exc
        return dst.read_bytes()


def _lz4_compress_legacy(data: bytes) -> bytes:
    """Compress to legacy-framed LZ4 via the external ``lz4`` CLI tool
    (``-l``), matching the framing Android's own ramdisk tooling uses
    (verified against real build artifacts: generic/vendor ramdisk
    sections start with the legacy frame magic ``02 21 4C 18``, not the
    modern LZ4 frame magic)."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in"
        dst = Path(td) / "out.lz4"
        src.write_bytes(data)
        try:
            subprocess.run(
                ["lz4", "-l", "-f", str(src), str(dst)],
                check=True, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BootImageError(f"lz4 compress failed: {exc}") from exc
        return dst.read_bytes()


def _find_cpio_trailer_offset(data: bytes) -> int:
    """Walk a plain (uncompressed) newc cpio stream by header fields to
    find the byte offset where its ``TRAILER!!!`` entry begins."""
    pos = 0
    while pos + 110 <= len(data):
        if data[pos:pos + 6] != b"070701":
            raise BootImageError(f"not a newc cpio stream at offset {pos}")
        fields = [
            int(data[pos + 6 + i * 8:pos + 6 + i * 8 + 8], 16) for i in range(13)
        ]
        namesize = fields[11]
        filesize = fields[6]
        name_start = pos + 6 + 13 * 8
        name = data[name_start:name_start + namesize].split(b"\x00", 1)[0]
        header_end = name_start + namesize
        header_end_padded = header_end + (-header_end) % 4
        if name == b"TRAILER!!!":
            return pos
        data_end = header_end_padded + filesize
        data_end_padded = data_end + (-data_end) % 4
        pos = data_end_padded
    raise BootImageError("cpio TRAILER!!! entry not found")


def _splice_into_ramdisk_cpio(existing_ramdisk: bytes, extra_ramdisk: bytes) -> bytes:
    """Insert ``extra_ramdisk``'s cpio entries into ``existing_ramdisk``'s
    own cpio stream, before its ``TRAILER!!!`` entry, preserving whatever
    compression ``existing_ramdisk`` already had.

    This exists because simply concatenating a *third* independent cpio
    archive after the vendor+generic pair is not reliably unpacked by the
    Linux kernel's early-userspace initramfs code in practice — verified
    by live first-stage-console tracing: a well-formed, correctly aligned
    third segment (plain, gzip, or even LZ4-compressed to match) causes
    the kernel to abandon cpio extraction partway through and instead
    preserve the unconsumed remainder as ``/initrd.image``, rather than
    continuing to unpack it. Splicing into the *existing* second segment
    and recompressing it as a single stream keeps the segment count at
    exactly two (vendor + generic), which is reliably supported, and was
    confirmed working end-to-end against real boot/init_boot/vendor_boot
    images.

    Raises BootImageError if ``existing_ramdisk``'s compression can't be
    recognized/round-tripped (currently: legacy-LZ4 or plain cpio only;
    LZ4 round-trip additionally requires the external ``lz4`` CLI tool).
    Callers should treat that as "best-effort enhancement unavailable"
    and fall back to not injecting ``extra_ramdisk``, not as fatal.
    """
    if _looks_like_legacy_lz4(existing_ramdisk):
        plain = _lz4_decompress(existing_ramdisk)
        recompress = True
    elif existing_ramdisk[:6] == b"070701":
        plain = existing_ramdisk
        recompress = False
    else:
        raise BootImageError(
            "ramdisk is neither legacy-LZ4 nor plain cpio; cannot splice into it"
        )

    trailer_off = _find_cpio_trailer_offset(plain)
    spliced_plain = plain[:trailer_off] + extra_ramdisk

    if recompress:
        return _lz4_compress_legacy(spliced_plain)
    return spliced_plain


def build_combined_initrd(
    boot: BootSections,
    init_boot: BootSections | None,
    vendor_boot: VendorBootSections | None,
    *,
    extra_ramdisk: bytes = b"",
) -> tuple[bytes, str]:
    """Combine boot/init_boot/vendor_boot sections into (initrd_bytes, cmdline).

    Combining order (matches the real bootloader's in-memory layout):
    vendor ramdisk(s) + generic ramdisk + bootconfig trailer.

    ``extra_ramdisk`` (optional, e.g. a synthetic ``/fstab.<hardware>``
    cpio archive) is not appended as a separate segment — it is spliced
    into the *generic* ramdisk's own cpio stream before its own trailer,
    and that stream is recompressed as a single unit, on a best-effort
    basis (see ``_splice_into_ramdisk_cpio``). The final bootconfig
    trailer's "#BOOTCONFIG\n" magic always remains the very last bytes of
    the initrd, as the kernel's bootconfig parser requires.

    Raises BootImageError if no usable kernel or ramdisk can be assembled.
    """

    if not boot.kernel:
        raise BootImageError(
            "boot.img has no embedded kernel (header v4 split image) — "
            "provide a kernel another way"
        )

    generic_ramdisk = b""
    if init_boot is not None and init_boot.ramdisk:
        generic_ramdisk = init_boot.ramdisk
    elif boot.ramdisk:
        generic_ramdisk = boot.ramdisk

    if not generic_ramdisk:
        raise BootImageError(
            "no generic ramdisk found in boot.img or init_boot.img"
        )

    vendor_ramdisk = vendor_boot.ramdisk if vendor_boot is not None else b""
    bootconfig_params = vendor_boot.bootconfig if vendor_boot is not None else b""
    bootconfig = _finish_bootconfig_trailer(bootconfig_params) if bootconfig_params else b""

    if extra_ramdisk:
        # Splice into the generic ramdisk's own cpio stream and recompress
        # it as a single stream, instead of appending a third independent
        # segment (which the kernel does not reliably unpack — see
        # _splice_into_ramdisk_cpio's docstring). Best-effort: if splicing
        # isn't possible in this environment (unrecognized compression,
        # missing external lz4 tool, …), proceed without the enhancement
        # rather than failing the whole native boot.img parse.
        try:
            generic_ramdisk = _splice_into_ramdisk_cpio(generic_ramdisk, extra_ramdisk)
        except BootImageError:
            pass

    combined = vendor_ramdisk + generic_ramdisk + bootconfig

    cmdline_parts = []
    if vendor_boot is not None and vendor_boot.cmdline:
        cmdline_parts.append(vendor_boot.cmdline)
    if boot.cmdline:
        cmdline_parts.append(boot.cmdline)
    combined_cmdline = " ".join(cmdline_parts)

    return combined, combined_cmdline


def pack_cpio_newc(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a minimal, uncompressed SVR4 "newc" ASCII cpio archive.

    Each entry is (name, content) for a plain regular file at the
    initramfs root (no leading ``/``). Ends with the standard
    ``TRAILER!!!`` marker. The Linux kernel's initramfs unpacker accepts
    multiple independently-compressed-or-not cpio archives concatenated
    back to back, so this uncompressed archive can be appended after
    compressed ramdisk segments without needing to touch their compression.
    """

    def _header(name: str, filesize: int, mode: int) -> bytes:
        name_bytes = name.encode("utf-8") + b"\x00"
        fields = (
            0,          # ino
            mode,       # mode
            0,          # uid
            0,          # gid
            1,          # nlink
            0,          # mtime
            filesize,   # filesize
            0,          # devmajor
            0,          # devminor
            0,          # rdevmajor
            0,          # rdevminor
            len(name_bytes),  # namesize
            0,          # check
        )
        header = b"070701" + "".join(f"{f:08X}" for f in fields).encode("ascii")
        return header + name_bytes

    def _pad4(data: bytes) -> bytes:
        pad = (-len(data)) % 4
        return data + b"\x00" * pad

    out = bytearray()
    for name, content in entries:
        out += _pad4(_header(name, len(content), 0o100644))
        out += _pad4(content)
    out += _pad4(_header("TRAILER!!!", 0, 0))
    return bytes(out)


_PCI_BOOT_DEVICE_PREFIX = "pci0000:00/0000:00:"
_PCI_SLOT_BASE = 0x10  # high, fixed slot range, clear of any QEMU default devices


def build_pci_boot_devices(count: int) -> tuple[str, list[int]]:
    """Build (androidboot.boot_devices cmdline value, [pci slot per drive]).

    fs_mgr/init only creates ``/dev/block/by-name/<partition>`` symlinks
    for a uevent whose sysfs PCI path is listed in ``androidboot.
    boot_devices`` (confirmed by reading system/core/init/devices.cpp:
    the by-name-from-partition_map symlink is gated behind
    ``info.is_boot_device``, which is exactly this comparison — it is
    *not* optional the way ``IsBootDeviceStrict()``'s separate "require
    partitions to be on the boot device" check is). The value is a
    comma-separated list of ``pci0000:00/0000:00:<slot_hex>.0`` sysfs
    path fragments, matching the format Cuttlefish's own qemu_manager.cpp
    (``ConfigureMultipleBootDevices``) generates.

    Each drive here is given an explicit, fixed PCI slot (starting at a
    high, unambiguous slot number well clear of any machine-default
    devices on PCI slots 0-2) via ``-device virtio-blk-pci,addr=0x..``,
    so the resulting sysfs path is deterministic instead of relying on
    whatever slot QEMU would have auto-assigned to an implicit
    ``if=virtio`` drive.
    """

    slots = [_PCI_SLOT_BASE + i for i in range(count)]
    boot_devices = ",".join(
        f"{_PCI_BOOT_DEVICE_PREFIX}{slot:02x}.0" for slot in slots
    )
    return boot_devices, slots


def build_partition_map_and_fstab(
    roles: list[str],
    *,
    hardware: str = "qemu",
) -> tuple[str, str, str]:
    """Build (partition_map_cmdline_value, fstab_filename, fstab_text) for a
    flat, GPT-less, one-partition-per-virtio-blk-disk layout (each drive is
    the *entire* content of one Android partition, matching how
    PRODUCT_OUT's per-partition .img files are normally built).

    ``roles`` is the ordered list of partition/mountpoint names (e.g.
    ``["system", "vendor", "data"]``) exactly as the caller attaches them
    via ``-drive if=virtio`` — virtio-blk device names (vda, vdb, vdc, ...)
    are assigned by the kernel in that same order, so this only works
    correctly if the *same* order is used for both.

    The special name "data" gets read-write f2fs/ext4 candidate fstab
    lines; everything else gets read-only erofs/ext4 candidate lines (the
    two most common Android partition filesystem types — fs_mgr tries each
    listed fs_type in turn until one mounts).
    """

    device_letters = [chr(ord("a") + i) for i in range(len(roles))]
    partition_map = ";".join(
        f"vd{letter},{name}" for letter, name in zip(device_letters, roles)
    )

    lines = ["# generated by AOSP_image_launcher for a flat virtio-blk-per-partition layout"]
    for name in roles:
        device = f"/dev/block/by-name/{name}"
        mount_point = f"/{name}"
        if name == "data":
            for fstype in ("f2fs", "ext4"):
                lines.append(
                    f"{device} /data {fstype} noatime,nosuid,nodev wait,first_stage_mount"
                )
        else:
            for fstype in ("erofs", "ext4"):
                lines.append(
                    f"{device} {mount_point} {fstype} ro wait,first_stage_mount"
                )

    fstab_text = "\n".join(lines) + "\n"
    fstab_filename = f"fstab.{hardware}"
    return partition_map, fstab_filename, fstab_text


_HARDWARE_PROP_RE = re.compile(r"androidboot\.hardware\s*=\s*\"?([\w.\-]+)\"?")


def detect_hardware_property(
    vendor_boot: "VendorBootSections | None",
    cmdline: str = "",
    *,
    default: str = "qemu",
) -> str:
    """Return the effective ``androidboot.hardware`` value.

    Android's bootconfig takes priority over the kernel cmdline for
    androidboot.* properties, so a vendor_boot.img bootconfig blob that
    already sets ``androidboot.hardware`` (common — e.g. Cuttlefish-built
    vendor_boot images set it to something like "cutf_cvm") silently wins
    over whatever we pass on ``-append``. fs_mgr looks for a file named
    ``/fstab.<that effective value>``, so our injected synthetic fstab must
    be named to match it, not whatever we asked for on the cmdline.
    """

    if vendor_boot is not None and vendor_boot.bootconfig:
        text = vendor_boot.bootconfig.decode("utf-8", errors="ignore")
        match = _HARDWARE_PROP_RE.search(text)
        if match:
            return match.group(1)
    match = _HARDWARE_PROP_RE.search(cmdline)
    if match:
        return match.group(1)
    return default
