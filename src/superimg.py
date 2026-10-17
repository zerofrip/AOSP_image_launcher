"""Native, stdlib-only parsing of Android liblp ``super.img`` metadata.

The parser reads only the small geometry + metadata header regions of a
potentially multi-gigabyte sparse or raw super image and exposes the
logical partition table.  Partition contents can then be extracted on
demand without loading the whole image into memory.

This implementation only supports single-super-block-device images, which
is the overwhelmingly common case for emulator/Cuttlefish/QEMU-style
builds.  Multi-block-device super.img layouts are parsed successfully, but
extracting a partition whose extents live on a non-primary block device
will raise ``SuperImageError`` instead of silently reading the wrong bytes.

Reference: AOSP ``system/core/fs_mgr/liblp`` — ``LpMetadataGeometry``,
``LpMetadataHeader``, ``LpMetadataPartition``, ``LpMetadataExtent`` and
``LpMetadataBlockDevice``.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from capabilities import discover_tools
from image_inspector import is_android_sparse

LP_SECTOR_SIZE = 512
LP_HEADER_MAGIC = 0x414C5030
LP_GEOMETRY_MAGIC = struct.unpack("<I", b"gDla")[0]
LP_TARGET_TYPE_LINEAR = 0

_PRIMARY_GEOMETRY_OFFSET = 4096
_BACKUP_GEOMETRY_OFFSET = 8192
_PRIMARY_METADATA_OFFSET = 12288

_GEOMETRY_FORMAT = "<II32sIII"  # 52 bytes
_GEOMETRY_SIZE = struct.calcsize(_GEOMETRY_FORMAT)

_PARTITION_FORMAT = "<36sIIII"  # 52 bytes
_PARTITION_SIZE = struct.calcsize(_PARTITION_FORMAT)

_EXTENT_FORMAT = "<QIQI"  # 24 bytes
_EXTENT_SIZE = struct.calcsize(_EXTENT_FORMAT)

_BLOCK_DEVICE_FIRST_SECTOR_SIZE = struct.calcsize("<Q")
_BLOCK_DEVICE_ALIGNMENT_SIZE = struct.calcsize("<I")

_HEADER_SIZE_MIN = 128


class SuperImageError(Exception):
    """Raised for an unreadable, truncated, or unsupported super image."""


def _round_up(value: int, align: int) -> int:
    return ((value + align - 1) // align) * align


def _require(data: bytes, size: int, what: str) -> None:
    if len(data) < size:
        raise SuperImageError(f"truncated {what}: need at least {size} bytes, got {len(data)}")


@dataclass
class _Extent:
    num_sectors: int
    target_type: int
    target_data: int
    target_source: int


@dataclass
class _Partition:
    name: str
    first_extent_index: int
    num_extents: int


@dataclass
class _BlockDevice:
    first_logical_sector: int
    alignment: int


@dataclass
class LogicalPartition:
    """A logical partition as recorded in the liblp metadata.

    ``name`` is the full stored name including any slot suffix (e.g.
    "system_a"). ``size`` is the total byte length of the partition,
    computed from the sum of its extents' ``num_sectors``.
    """

    name: str
    size: int


@dataclass
class SuperMetadata:
    """Parsed liblp metadata geometry and partition table.

    ``partitions`` lists the logical partition names exactly as stored in
    the metadata (e.g. ``"system_a"``, ``"vendor_b"``).
    """

    metadata_max_size: int
    metadata_slot_count: int
    header_version: tuple[int, int]
    logical_block_size: int
    partitions: list[LogicalPartition]

    @property
    def major_version(self) -> int:
        """Backwards-compatible alias for ``header_version[0]``."""
        return self.header_version[0]

    @property
    def minor_version(self) -> int:
        """Backwards-compatible alias for ``header_version[1]``."""
        return self.header_version[1]

    @property
    def block_device_count(self) -> int:
        """Number of physical block devices declared in the metadata."""
        devices = getattr(self, "_block_devices", None)
        return len(devices) if devices is not None else 0


def available_slot_suffixes(metadata: SuperMetadata) -> list[str]:
    """Return the distinct slot suffixes observed across all partition
    names (e.g. ``["_a", "_b"]``), sorted for determinism.

    Only a trailing ``_<single lowercase letter>`` component is
    considered a slot suffix; names like ``system_ext`` (where the
    trailing component after the last ``_`` is longer than one
    character) are correctly excluded. Returns ``[""]`` if no
    partition name has such a suffix (non-A/B image).
    """
    suffixes: set[str] = set()
    for partition in metadata.partitions:
        before, sep, suffix = partition.name.rpartition("_")
        if before and sep and len(suffix) == 1 and suffix.isalpha() and suffix.islower():
            suffixes.add(f"_{suffix}")
    return sorted(suffixes) if suffixes else [""]


def _read_geometry(fh, offset: int) -> tuple[int, int, int, int]:
    """Return (struct_size, metadata_max_size, metadata_slot_count, logical_block_size)."""

    try:
        fh.seek(offset)
        raw = fh.read(_GEOMETRY_SIZE)
    except OSError as exc:
        raise SuperImageError(f"cannot read geometry at offset {offset}: {exc}") from exc

    _require(raw, _GEOMETRY_SIZE, f"geometry struct at offset {offset}")
    magic, struct_size, _checksum, metadata_max_size, metadata_slot_count, logical_block_size = (
        struct.unpack_from(_GEOMETRY_FORMAT, raw, 0)
    )
    if magic != LP_GEOMETRY_MAGIC:
        raise SuperImageError(
            f"bad geometry magic at offset {offset}: expected 0x{LP_GEOMETRY_MAGIC:08x}, "
            f"got 0x{magic:08x}"
        )
    if struct_size != _GEOMETRY_SIZE:
        raise SuperImageError(
            f"unsupported geometry struct_size {struct_size} at offset {offset}, "
            f"expected {_GEOMETRY_SIZE}"
        )
    if metadata_slot_count == 0 or metadata_max_size == 0:
        raise SuperImageError(
            f"invalid geometry at offset {offset}: metadata_max_size={metadata_max_size}, "
            f"metadata_slot_count={metadata_slot_count}"
        )
    return struct_size, metadata_max_size, metadata_slot_count, logical_block_size


def _read_header(fh, metadata_copy_start: int) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int]:
    """Read and validate the LpMetadataHeader.

    Returns:
        (major, minor, header_size, tables_size,
         part_td_off, part_td_num, part_td_esz,
         ext_td_off, ext_td_num, ext_td_esz,
         bd_td_off, bd_td_num, bd_td_esz)
    """

    try:
        fh.seek(metadata_copy_start)
        header_raw = fh.read(_HEADER_SIZE_MIN)
    except OSError as exc:
        raise SuperImageError(f"cannot read metadata header: {exc}") from exc

    _require(header_raw, _HEADER_SIZE_MIN, "metadata header")

    magic, = struct.unpack_from("<I", header_raw, 0)
    major, minor = struct.unpack_from("<HH", header_raw, 4)
    header_size, = struct.unpack_from("<I", header_raw, 8)
    tables_size, = struct.unpack_from("<I", header_raw, 44)

    part_td_off, part_td_num, part_td_esz = struct.unpack_from("<III", header_raw, 80)
    ext_td_off, ext_td_num, ext_td_esz = struct.unpack_from("<III", header_raw, 92)
    grp_td_off, grp_td_num, grp_td_esz = struct.unpack_from("<III", header_raw, 104)
    bd_td_off, bd_td_num, bd_td_esz = struct.unpack_from("<III", header_raw, 116)

    if magic != LP_HEADER_MAGIC:
        raise SuperImageError(
            f"bad metadata header magic: expected 0x{LP_HEADER_MAGIC:08x}, got 0x{magic:08x}"
        )
    if header_size < _HEADER_SIZE_MIN:
        raise SuperImageError(f"metadata header_size {header_size} is too small")
    expected_tables_size = (
        part_td_num * part_td_esz
        + ext_td_num * ext_td_esz
        + grp_td_num * grp_td_esz
        + bd_td_num * bd_td_esz
    )
    if tables_size != expected_tables_size:
        raise SuperImageError(
            f"metadata tables_size mismatch: header says {tables_size}, "
            f"descriptor sum is {expected_tables_size}"
        )

    return (
        major,
        minor,
        header_size,
        tables_size,
        part_td_off,
        part_td_num,
        part_td_esz,
        ext_td_off,
        ext_td_num,
        ext_td_esz,
        bd_td_off,
        bd_td_num,
        bd_td_esz,
    )


def _read_extents(raw: bytes, offset: int, count: int, entry_size: int) -> list[_Extent]:
    extents: list[_Extent] = []
    for i in range(count):
        pos = offset + i * entry_size
        _require(raw, pos + _EXTENT_SIZE, f"extent table entry {i}")
        num_sectors, target_type, target_data, target_source = struct.unpack_from(
            _EXTENT_FORMAT, raw, pos
        )
        extents.append(
            _Extent(
                num_sectors=num_sectors,
                target_type=target_type,
                target_data=target_data,
                target_source=target_source,
            )
        )
    return extents


def _read_partitions(raw: bytes, offset: int, count: int, entry_size: int) -> list[_Partition]:
    partitions: list[_Partition] = []
    for i in range(count):
        pos = offset + i * entry_size
        _require(raw, pos + _PARTITION_SIZE, f"partition table entry {i}")
        name_raw, _attributes, first_extent_index, num_extents, _group_index = (
            struct.unpack_from(_PARTITION_FORMAT, raw, pos)
        )
        name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        partitions.append(
            _Partition(
                name=name,
                first_extent_index=first_extent_index,
                num_extents=num_extents,
            )
        )
    return partitions


def _read_block_devices(raw: bytes, offset: int, count: int, entry_size: int) -> list[_BlockDevice]:
    devices: list[_BlockDevice] = []
    for i in range(count):
        pos = offset + i * entry_size
        _require(
            raw,
            pos + _BLOCK_DEVICE_FIRST_SECTOR_SIZE + _BLOCK_DEVICE_ALIGNMENT_SIZE,
            f"block device table entry {i}",
        )
        first_logical_sector, = struct.unpack_from("<Q", raw, pos)
        alignment, = struct.unpack_from("<I", raw, pos + 8)
        devices.append(
            _BlockDevice(
                first_logical_sector=first_logical_sector,
                alignment=alignment,
            )
        )
    return devices


def _find_simg2img(simg2img: Path | None) -> Path:
    """Resolve the simg2img executable to use for sparse conversion.

    An explicit path is trusted as-is.  Otherwise the existing
    capabilities probe is reused.
    """
    if simg2img is not None:
        return simg2img
    tool = discover_tools().simg2img
    if not tool.available or tool.path is None:
        raise SuperImageError(
            "super.img is in Android sparse format but simg2img is not available; "
            "install simg2img (e.g. from AOSP host tools) to convert it"
        )
    return tool.path


def _convert_sparse(sparse_path: Path, simg2img: Path) -> Path:
    """Convert ``sparse_path`` to a temporary raw image and return its path.

    The caller is responsible for deleting the returned file.
    """
    try:
        handle = tempfile.NamedTemporaryFile(suffix=".img", delete=False)
        raw_path = Path(handle.name)
        handle.close()
    except OSError as exc:
        raise SuperImageError(
            f"cannot create temporary raw image for {sparse_path}: {exc}"
        ) from exc

    try:
        subprocess.run(
            [os.fspath(simg2img), os.fspath(sparse_path), os.fspath(raw_path)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        try:
            raw_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SuperImageError(
            f"simg2img failed to convert sparse {sparse_path}: {exc}"
        ) from exc
    return raw_path


def _raw_image_path(path: Path, simg2img: Path | None) -> tuple[Path, Path | None]:
    """Return the path to open and an optional temporary raw file to clean up."""
    if is_android_sparse(path):
        tool_path = _find_simg2img(simg2img)
        raw_path = _convert_sparse(path, tool_path)
        return raw_path, raw_path
    return path, None


def read_super_metadata(path: Path, *, simg2img: Path | None = None) -> SuperMetadata:
    """Parse the liblp geometry and metadata header from ``path``.

    Only the geometry blocks and the first metadata copy are read; the
    potentially large partition payloads are left on disk.
    """

    raw_path, tmp_path = _raw_image_path(path, simg2img)
    try:
        try:
            fh = open(raw_path, "rb")
        except OSError as exc:
            raise SuperImageError(f"cannot open {path}: {exc}") from exc

        try:
            try:
                _, metadata_max_size, metadata_slot_count, logical_block_size = _read_geometry(
                    fh, _PRIMARY_GEOMETRY_OFFSET
                )
            except SuperImageError:
                _, metadata_max_size, metadata_slot_count, logical_block_size = _read_geometry(
                    fh, _BACKUP_GEOMETRY_OFFSET
                )

            metadata_copy_start = _PRIMARY_METADATA_OFFSET
            (
                major,
                minor,
                header_size,
                tables_size,
                part_td_off,
                part_td_num,
                part_td_esz,
                ext_td_off,
                ext_td_num,
                ext_td_esz,
                bd_td_off,
                bd_td_num,
                bd_td_esz,
            ) = _read_header(fh, metadata_copy_start)

            tables_start = metadata_copy_start + header_size
            try:
                fh.seek(tables_start)
                tables_raw = fh.read(tables_size)
            except OSError as exc:
                raise SuperImageError(f"cannot read metadata tables: {exc}") from exc
            _require(tables_raw, tables_size, "metadata tables")

            partitions = _read_partitions(
                tables_raw, part_td_off, part_td_num, part_td_esz
            )
            extents = _read_extents(tables_raw, ext_td_off, ext_td_num, ext_td_esz)
            block_devices = _read_block_devices(
                tables_raw, bd_td_off, bd_td_num, bd_td_esz
            )

            # Validate extent references now that both tables are loaded.
            for p in partitions:
                if p.first_extent_index < 0 or p.first_extent_index + p.num_extents > len(extents):
                    raise SuperImageError(
                        f"partition {p.name!r} has out-of-range extent indices"
                    )

            # Compute each partition's total size from its extents, in bytes.
            logical_partitions: list[LogicalPartition] = []
            for p in partitions:
                total_sectors = sum(
                    extents[p.first_extent_index + i].num_sectors for i in range(p.num_extents)
                )
                logical_partitions.append(
                    LogicalPartition(name=p.name, size=total_sectors * LP_SECTOR_SIZE)
                )

            # Stash references on the metadata object for extraction.
            metadata = SuperMetadata(
                metadata_max_size=metadata_max_size,
                metadata_slot_count=metadata_slot_count,
                header_version=(major, minor),
                logical_block_size=logical_block_size,
                partitions=logical_partitions,
            )
            object.__setattr__(metadata, "_partitions", partitions)
            object.__setattr__(metadata, "_extents", extents)
            object.__setattr__(metadata, "_block_devices", block_devices)
            return metadata
        finally:
            fh.close()
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _get_partition(metadata: SuperMetadata, base_name: str, slot_suffix: str) -> _Partition:
    target_name = base_name + slot_suffix
    for p in metadata._partitions:  # type: ignore[attr-defined]
        if p.name == target_name:
            return p
    raise SuperImageError(f"partition {target_name!r} not found")


def _get_block_device(metadata: SuperMetadata, index: int) -> _BlockDevice:
    devices = metadata._block_devices  # type: ignore[attr-defined]
    if index < 0 or index >= len(devices):
        raise SuperImageError(f"block device index {index} out of range")
    return devices[index]


def extract_partition(
    super_path: Path,
    metadata: SuperMetadata,
    base_name: str,
    slot_suffix: str = "",
    *,
    simg2img: Path | None = None,
) -> bytes:
    """Extract the raw bytes of logical partition ``base_name + slot_suffix``.

    Only the sectors referenced by the partition's extents are read from
    ``super_path``; the rest of the file is not loaded.
    """

    partition = _get_partition(metadata, base_name, slot_suffix)
    extents = metadata._extents  # type: ignore[attr-defined]

    raw_path, tmp_path = _raw_image_path(super_path, simg2img)
    try:
        try:
            fh = open(raw_path, "rb")
        except OSError as exc:
            raise SuperImageError(f"cannot open {super_path}: {exc}") from exc

        try:
            out = bytearray()
            for i in range(partition.num_extents):
                extent = extents[partition.first_extent_index + i]
                if extent.target_type != LP_TARGET_TYPE_LINEAR:
                    raise SuperImageError(
                        f"partition {partition.name!r} extent {i} has unsupported target type "
                        f"{extent.target_type}"
                    )
                # ``target_source`` identifies which block device the extent lives
                # on.  This implementation only supports the primary block device
                # (index 0); fail clearly for any other device rather than read
                # bytes from the wrong file/location.
                _get_block_device(metadata, extent.target_source)
                if extent.target_source != 0:
                    raise SuperImageError(
                        f"partition {partition.name!r} extent {i} lives on block "
                        f"device {extent.target_source}, but this implementation "
                        f"only supports the primary block device (index 0)"
                    )
                # ``target_data`` already is the absolute sector number on the
                # target block device (confirmed against a real built super.img);
                # it must NOT be added to the device's first_logical_sector.
                byte_offset = extent.target_data * LP_SECTOR_SIZE
                byte_length = extent.num_sectors * LP_SECTOR_SIZE
                try:
                    fh.seek(byte_offset)
                    chunk = fh.read(byte_length)
                except OSError as exc:
                    raise SuperImageError(
                        f"cannot read partition {partition.name!r} extent {i}: {exc}"
                    ) from exc
                if len(chunk) < byte_length:
                    raise SuperImageError(
                        f"short read for partition {partition.name!r} extent {i}: "
                        f"expected {byte_length} bytes, got {len(chunk)}"
                    )
                out += chunk
            return bytes(out)
        finally:
            fh.close()
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def extract_partition_to_file(
    super_path: Path,
    metadata: SuperMetadata,
    base_name: str,
    slot_suffix: str = "",
    dest_dir: Path = Path("."),
    *,
    simg2img: Path | None = None,
) -> Path:
    """Extract a logical partition and write it to ``dest_dir/<name>.img``."""

    data = extract_partition(
        super_path, metadata, base_name, slot_suffix, simg2img=simg2img
    )
    # Output filename never includes the slot suffix: the caller already
    # knows which slot it asked for, and the result represents "the
    # system/vendor image to use", not "the system_a partition specifically".
    dest_path = Path(dest_dir) / f"{base_name}.img"
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
    except OSError as exc:
        raise SuperImageError(f"cannot write {dest_path}: {exc}") from exc
    return dest_path
