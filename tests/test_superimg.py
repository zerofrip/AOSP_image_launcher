"""Unit tests for src/superimg.py: native liblp super.img parsing."""

from __future__ import annotations

import struct
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import support  # noqa: F401
import superimg  # noqa: F401  # patched directly in sparse tests

from capabilities import ToolInfo
from superimg import (
    LP_SECTOR_SIZE,
    LogicalPartition,
    SuperImageError,
    SuperMetadata,
    available_slot_suffixes,
    extract_partition,
    extract_partition_to_file,
    read_super_metadata,
)

_LP_HEADER_MAGIC = 0x414C5030
_LP_GEOMETRY_MAGIC_BYTES = b"gDla"
_LP_GEOMETRY_MAGIC = struct.unpack("<I", _LP_GEOMETRY_MAGIC_BYTES)[0]

# Fixture format strings — independent of superimg.py's internals.
_T_GEOM_FMT = "<II32sIII"       # 52 bytes
_T_PART_FMT = "<36sIIII"        # 52 bytes
_T_EXTENT_FMT = "<QIQI"         # 24 bytes
_T_BD_FMT = "<QI"               # 12 bytes
_T_DESC_FMT = "<III"            # 12 bytes
_BD_ENTRY_SIZE = 48

_PRIMARY_GEOMETRY_OFFSET = 4096
_BACKUP_GEOMETRY_OFFSET = 8192
_PRIMARY_METADATA_OFFSET = 12288


def _round_up(n: int, align: int) -> int:
    return ((n + align - 1) // align) * align


def _write(tmpdir: str, name: str, data: bytes) -> Path:
    path = Path(tmpdir) / name
    path.write_bytes(data)
    return path


def _fake_simg2img_runner(raw_image: bytes):
    """Return a stand-in subprocess.run that writes ``raw_image`` to argv[2]."""

    def run(argv, **_kwargs):
        Path(argv[2]).write_bytes(raw_image)
        return subprocess.CompletedProcess(argv, returncode=0, stdout=b"", stderr=b"")

    return run


def _encode_name(name: str) -> bytes:
    return name.encode("utf-8")[:35].ljust(36, b"\x00")[:36]


def _build_super_image_raw(
    payload_map: dict[str, bytes],
    *,
    metadata_max_size: int | None = None,
    metadata_slot_count: int = 2,
    logical_block_size: int = 4096,
    corrupt_primary_geometry: bool = False,
    num_block_devices: int = 1,
    target_sources: dict[str, int] | None = None,
) -> bytes:
    """Construct a spec-correct liblp super image from scratch.

    ``payload_map`` maps partition names (including any slot suffix) to
    their raw payload bytes.  Each partition gets one linear extent.

    ``target_sources`` optionally overrides the block-device index for a
    named partition's extent; partitions not present default to device 0.
    """

    num_partitions = len(payload_map)
    num_extents = num_partitions
    num_groups = 0

    part_table_size = num_partitions * struct.calcsize(_T_PART_FMT)
    ext_table_size = num_extents * struct.calcsize(_T_EXTENT_FMT)
    grp_table_size = num_groups * 0
    bd_table_size = num_block_devices * _BD_ENTRY_SIZE
    tables_size = part_table_size + ext_table_size + grp_table_size + bd_table_size

    if metadata_max_size is None:
        metadata_max_size = _round_up(128 + tables_size, 4096)

    # Place payloads immediately after the metadata region.
    data_start = _PRIMARY_METADATA_OFFSET + metadata_max_size * metadata_slot_count * 2

    # Compute sector-aligned payload layout.
    # target_data stores absolute file sectors; partition data starts at
    # data_start, so absolute_sector = data_start//512 + relative_sector.
    data_start_sector = data_start // LP_SECTOR_SIZE
    extent_infos: list[tuple[str, bytes, int, int, int]] = []
    next_relative_sector = 0
    for name, payload in sorted(payload_map.items()):
        num_sectors = _round_up(len(payload), LP_SECTOR_SIZE) // LP_SECTOR_SIZE
        absolute_sector = data_start_sector + next_relative_sector
        extent_infos.append((name, payload, next_relative_sector, num_sectors, absolute_sector))
        next_relative_sector += num_sectors
    total_data_sectors = next_relative_sector

    image_size = data_start + total_data_sectors * LP_SECTOR_SIZE
    image = bytearray(image_size)

    # Geometry.
    def _geometry() -> bytes:
        return struct.pack(
            _T_GEOM_FMT,
            _LP_GEOMETRY_MAGIC,
            struct.calcsize(_T_GEOM_FMT),
            b"\x00" * 32,
            metadata_max_size,
            metadata_slot_count,
            logical_block_size,
        )

    geom = _geometry()
    image[_PRIMARY_GEOMETRY_OFFSET:_PRIMARY_GEOMETRY_OFFSET + len(geom)] = geom
    image[_BACKUP_GEOMETRY_OFFSET:_BACKUP_GEOMETRY_OFFSET + len(geom)] = geom
    if corrupt_primary_geometry:
        image[_PRIMARY_GEOMETRY_OFFSET + 0:_PRIMARY_GEOMETRY_OFFSET + 4] = b"BAD!"

    # Header at the primary metadata copy.
    header = bytearray(128)
    struct.pack_into("<I", header, 0, _LP_HEADER_MAGIC)
    struct.pack_into("<H", header, 4, 10)          # major_version
    struct.pack_into("<H", header, 6, 0)           # minor_version
    struct.pack_into("<I", header, 8, 128)         # header_size
    # bytes 12..43: header_checksum = zeros
    struct.pack_into("<I", header, 44, tables_size)
    # bytes 48..79: tables_checksum = zeros
    tables_start = _PRIMARY_METADATA_OFFSET + 128
    part_td_off = 0
    ext_td_off = part_table_size
    grp_td_off = ext_td_off + ext_table_size
    bd_td_off = grp_td_off + grp_table_size
    struct.pack_into("<III", header, 80, part_td_off, num_partitions, struct.calcsize(_T_PART_FMT))
    struct.pack_into("<III", header, 92, ext_td_off, num_extents, struct.calcsize(_T_EXTENT_FMT))
    struct.pack_into("<III", header, 104, grp_td_off, num_groups, 0)
    struct.pack_into("<III", header, 116, bd_td_off, num_block_devices, _BD_ENTRY_SIZE)
    image[_PRIMARY_METADATA_OFFSET:_PRIMARY_METADATA_OFFSET + 128] = header

    # Partition table.
    for i, (name, _payload, _rel_sector, _num_sectors, _abs_sector) in enumerate(extent_infos):
        offset = tables_start + part_td_off + i * struct.calcsize(_T_PART_FMT)
        struct.pack_into(
            _T_PART_FMT,
            image,
            offset,
            _encode_name(name),
            0,      # attributes
            i,      # first_extent_index
            1,      # num_extents
            0,      # group_index
        )

    # Extent table.
    target_sources = target_sources or {}
    for i, (name, _payload, _rel_sector, num_sectors, absolute_sector) in enumerate(extent_infos):
        offset = tables_start + ext_td_off + i * struct.calcsize(_T_EXTENT_FMT)
        struct.pack_into(
            _T_EXTENT_FMT,
            image,
            offset,
            num_sectors,
            0,                            # target_type = LINEAR
            absolute_sector,              # target_data = absolute file sector
            target_sources.get(name, 0),  # target_source = block device index
        )

    # Block device table.
    for i in range(num_block_devices):
        offset = tables_start + bd_td_off + i * _BD_ENTRY_SIZE
        struct.pack_into(
            _T_BD_FMT,
            image,
            offset,
            0 if i == 0 else 0xFFFFFFFF,  # first_logical_sector
            0,                            # alignment
        )

    # Payloads.
    for _name, payload, rel_sector, _num_sectors, _abs_sector in extent_infos:
        byte_offset = data_start + rel_sector * LP_SECTOR_SIZE
        image[byte_offset:byte_offset + len(payload)] = payload

    return bytes(image)


class TestStructSizes(unittest.TestCase):
    def test_geometry_struct_size(self) -> None:
        """Geometry struct must be exactly 52 bytes as per the spec."""
        self.assertEqual(struct.calcsize(_T_GEOM_FMT), 52)

    def test_partition_struct_size(self) -> None:
        self.assertEqual(struct.calcsize(_T_PART_FMT), 52)

    def test_extent_struct_size(self) -> None:
        self.assertEqual(struct.calcsize(_T_EXTENT_FMT), 24)


class TestRoundTrip(unittest.TestCase):
    def test_round_trip_two_partitions(self) -> None:
        system_payload = b"SYSTEM" + b"\x00" * (LP_SECTOR_SIZE - 6)
        vendor_payload = b"VENDOR" * 85  # 510 bytes, deliberately not sector-aligned
        expected_vendor = vendor_payload + b"\x00" * 2  # extraction returns full sectors

        data = _build_super_image_raw({
            "system_a": system_payload,
            "vendor_b": vendor_payload,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)

            self.assertIsInstance(metadata, SuperMetadata)
            self.assertEqual(metadata.header_version, (10, 0))
            self.assertEqual(metadata.major_version, 10)
            self.assertEqual(metadata.minor_version, 0)
            self.assertGreater(metadata.metadata_max_size, 128)
            self.assertEqual(metadata.metadata_slot_count, 2)
            names = sorted(p.name for p in metadata.partitions)
            self.assertEqual(names, ["system_a", "vendor_b"])
            system_part = next(p for p in metadata.partitions if p.name == "system_a")
            vendor_part = next(p for p in metadata.partitions if p.name == "vendor_b")
            self.assertIsInstance(system_part, LogicalPartition)
            self.assertEqual(system_part.size, len(system_payload))
            self.assertEqual(vendor_part.size, len(expected_vendor))

            self.assertEqual(extract_partition(path, metadata, "system", "_a"), system_payload)
            self.assertEqual(extract_partition(path, metadata, "vendor", "_b"), expected_vendor)

    def test_metadata_max_size_round_trips(self) -> None:
        data = _build_super_image_raw({"system": b"x" * 512}, metadata_max_size=0x10000)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(metadata.metadata_max_size, 0x10000)


class TestSlotSuffixes(unittest.TestCase):
    def test_available_slot_suffixes(self) -> None:
        data = _build_super_image_raw({
            "system": b"s" * 512,
            "vendor": b"v" * 512,
            "system_ext": b"e" * 512,
            "system_a": b"a" * 512,
            "vendor_b": b"b" * 512,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(available_slot_suffixes(metadata), ["_a", "_b"])

    def test_available_slot_suffixes_empty_for_non_ab_image(self) -> None:
        data = _build_super_image_raw({
            "system": b"s" * 512,
            "vendor": b"v" * 512,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(available_slot_suffixes(metadata), [""])

    def test_available_slot_suffixes_ignores_system_ext(self) -> None:
        data = _build_super_image_raw({
            "system_ext": b"e" * 512,
            "vendor_dlkm": b"d" * 512,
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(available_slot_suffixes(metadata), [""])


class TestGeometryFallback(unittest.TestCase):
    def test_backup_geometry_used_when_primary_corrupt(self) -> None:
        data = _build_super_image_raw(
            {"system": b"sys" * 170 + b"t" * 2},  # 512 bytes
            corrupt_primary_geometry=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual([p.name for p in metadata.partitions], ["system"])


class TestMultiExtent(unittest.TestCase):
    def test_multi_extent_partition_reads_all_extents(self) -> None:
        # Manually build a partition with three extents to exercise sector
        # accumulation in extract_partition.
        chunks = [b"A" * LP_SECTOR_SIZE, b"B" * LP_SECTOR_SIZE, b"C" * LP_SECTOR_SIZE]
        payload = b"".join(chunks)
        data = _build_super_image_raw({"system": payload})

        # Re-open and patch the single extent into three extents, manually.
        image = bytearray(data)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "super.img"

            # Recompute layout constants from the helper above.
            num_partitions = 1
            part_table_size = num_partitions * struct.calcsize(_T_PART_FMT)
            ext_table_size = 3 * struct.calcsize(_T_EXTENT_FMT)
            tables_size = part_table_size + ext_table_size + _BD_ENTRY_SIZE
            metadata_max_size = _round_up(128 + tables_size, 4096)
            data_start = _PRIMARY_METADATA_OFFSET + metadata_max_size * 2 * 2

            # Rewrite header tables_size.
            struct.pack_into("<I", image, _PRIMARY_METADATA_OFFSET + 44, tables_size)

            tables_start = _PRIMARY_METADATA_OFFSET + 128
            part_td_off = 0
            ext_td_off = part_table_size
            grp_td_off = ext_td_off + ext_table_size
            bd_td_off = grp_td_off
            struct.pack_into("<III", image, _PRIMARY_METADATA_OFFSET + 92, ext_td_off, 3, 24)
            struct.pack_into(
                "<III", image, _PRIMARY_METADATA_OFFSET + 116, bd_td_off, 1, _BD_ENTRY_SIZE
            )

            # Update partition to use 3 extents.
            struct.pack_into(
                _T_PART_FMT,
                image,
                tables_start + part_td_off,
                _encode_name("system"),
                0,
                0,
                3,
                0,
            )

            # Rewrite extents with absolute sector numbers.
            data_start_sector = data_start // LP_SECTOR_SIZE
            for i, chunk in enumerate(chunks):
                sector = data_start_sector + i
                struct.pack_into(
                    _T_EXTENT_FMT,
                    image,
                    tables_start + ext_td_off + i * struct.calcsize(_T_EXTENT_FMT),
                    1,
                    0,
                    sector,
                    0,
                )
                byte_offset = data_start + i * LP_SECTOR_SIZE
                image[byte_offset:byte_offset + len(chunk)] = chunk

            path.write_bytes(bytes(image))
            metadata = read_super_metadata(path)
            self.assertEqual(extract_partition(path, metadata, "system", ""), payload)


class TestExtractionErrors(unittest.TestCase):
    def test_extract_wrong_partition_raises(self) -> None:
        data = _build_super_image_raw({"system": b"x" * 512})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            with self.assertRaises(SuperImageError):
                extract_partition(path, metadata, "vendor", "")

    def test_extract_partition_to_file(self) -> None:
        payload = b"to_file" + b"\x00" * (LP_SECTOR_SIZE - 7)
        data = _build_super_image_raw({"vendor_a": payload})
        with tempfile.TemporaryDirectory() as tmpdir:
            super_path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(super_path)
            dest_dir = Path(tmpdir) / "out"
            out_path = extract_partition_to_file(
                super_path, metadata, "vendor", "_a", dest_dir
            )
            self.assertEqual(out_path, dest_dir / "vendor.img")
            self.assertFalse((dest_dir / "vendor_a.img").exists())
            self.assertEqual(out_path.read_bytes(), payload)

    def test_bad_geometry_magic_raises(self) -> None:
        data = bytearray(_build_super_image_raw({"system": b"x" * 512}))
        # Corrupt both geometry magic fields.
        data[_PRIMARY_GEOMETRY_OFFSET:_PRIMARY_GEOMETRY_OFFSET + 4] = b"BAD!"
        data[_BACKUP_GEOMETRY_OFFSET:_BACKUP_GEOMETRY_OFFSET + 4] = b"BAD!"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", bytes(data))
            with self.assertRaises(SuperImageError):
                read_super_metadata(path)

    def test_bad_header_magic_raises(self) -> None:
        data = bytearray(_build_super_image_raw({"system": b"x" * 512}))
        data[_PRIMARY_METADATA_OFFSET + 0:_PRIMARY_METADATA_OFFSET + 4] = b"BAD!"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", bytes(data))
            with self.assertRaises(SuperImageError):
                read_super_metadata(path)

    def test_tables_size_mismatch_raises(self) -> None:
        data = bytearray(_build_super_image_raw({"system": b"x" * 512}))
        # Lie about tables_size in the header.
        struct.pack_into("<I", data, _PRIMARY_METADATA_OFFSET + 44, 0xFFFFFFFF)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", bytes(data))
            with self.assertRaises(SuperImageError):
                read_super_metadata(path)

    def test_non_linear_extent_raises(self) -> None:
        """A ZERO (target_type=1) extent on the requested partition must
        raise SuperImageError naming the unsupported target type, not be
        silently treated as a hole or zero-filled."""
        data = bytearray(_build_super_image_raw({"system": b"x" * 512}))
        tables_start = _PRIMARY_METADATA_OFFSET + 128
        # The single extent for "system" is the first (and only) entry in
        # the extent table, which immediately follows the partition table
        # (one entry, 52 bytes) in _build_super_image_raw's layout.
        ext_table_offset = tables_start + struct.calcsize(_T_PART_FMT)
        # target_type is the uint32 at byte offset 8 within the extent entry.
        struct.pack_into("<I", data, ext_table_offset + 8, 1)  # LP_TARGET_TYPE_ZERO
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", bytes(data))
            metadata = read_super_metadata(path)
            with self.assertRaisesRegex(SuperImageError, "target type"):
                extract_partition(path, metadata, "system", "")

    def test_truncated_file_raises_not_struct_error(self) -> None:
        """A file cut off after the geometry blocks but before the full
        metadata header must raise SuperImageError, never a raw
        struct.error."""
        data = _build_super_image_raw({"system": b"x" * 512})
        truncated = data[:_PRIMARY_METADATA_OFFSET + 64]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", truncated)
            with self.assertRaises(SuperImageError):
                read_super_metadata(path)


class TestSparseHandling(unittest.TestCase):
    def test_raw_super_image_still_parses(self) -> None:
        """A non-sparse raw super image must use the existing byte-for-byte
        parsing path with no conversion."""
        payload = b"RAW" + b"\x00" * (LP_SECTOR_SIZE - 3)
        raw_data = _build_super_image_raw({"system_a": payload})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", raw_data)
            metadata = read_super_metadata(path)
            self.assertEqual([p.name for p in metadata.partitions], ["system_a"])
            self.assertEqual(extract_partition(path, metadata, "system", "_a"), payload)

    def test_sparse_super_image_is_converted_before_parsing(self) -> None:
        """A sparse-format super.img is detected, simg2img is invoked, and
        parsing/extraction operate on the temporary raw copy."""
        payload = b"SPARSE" + b"\x00" * (LP_SECTOR_SIZE - 6)
        raw_data = _build_super_image_raw({"system_a": payload})
        sparse_data = support.sparse_header()
        with tempfile.TemporaryDirectory() as tmpdir:
            sparse_path = _write(tmpdir, "super.img", sparse_data)
            fake_simg2img = Path(tmpdir) / "simg2img"
            fake_simg2img.write_text("#!/bin/sh\n# mock")
            with unittest.mock.patch(
                "superimg.subprocess.run", side_effect=_fake_simg2img_runner(raw_data)
            ):
                metadata = read_super_metadata(sparse_path, simg2img=fake_simg2img)
                self.assertEqual([p.name for p in metadata.partitions], ["system_a"])
                self.assertEqual(
                    extract_partition(
                        sparse_path, metadata, "system", "_a", simg2img=fake_simg2img
                    ),
                    payload,
                )

    def test_sparse_super_image_without_simg2img_raises(self) -> None:
        """A sparse super.img with no simg2img available must raise a clear
        SuperImageError rather than a confusing raw parse failure."""

        class _Caps:
            pass

        caps = _Caps()
        caps.simg2img = ToolInfo(
            name="simg2img", path=None, available=False, error="not found"
        )
        sparse_data = support.sparse_header()
        with tempfile.TemporaryDirectory() as tmpdir:
            sparse_path = _write(tmpdir, "super.img", sparse_data)
            with unittest.mock.patch.object(
                superimg, "discover_tools", return_value=caps
            ):
                with self.assertRaisesRegex(SuperImageError, "sparse.*simg2img"):
                    read_super_metadata(sparse_path)


class TestBlockDeviceHandling(unittest.TestCase):
    def test_single_block_device_behavior_unchanged(self) -> None:
        """A single-block-device image must parse and extract exactly as before."""
        payload = b"SINGLE" + b"\x00" * (LP_SECTOR_SIZE - 6)
        data = _build_super_image_raw({"system_a": payload})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(metadata.block_device_count, 1)
            self.assertEqual(extract_partition(path, metadata, "system", "_a"), payload)

    def test_multi_block_device_all_extents_on_zero_extracts(self) -> None:
        """Metadata with multiple block devices still extracts partitions whose
        extents all live on the primary (index 0) block device."""
        payload = b"PRIMARY" + b"\x00" * (LP_SECTOR_SIZE - 7)
        data = _build_super_image_raw(
            {"system_a": payload},
            num_block_devices=2,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(metadata.block_device_count, 2)
            self.assertEqual(extract_partition(path, metadata, "system", "_a"), payload)

    def test_multi_block_device_non_zero_source_raises(self) -> None:
        """A partition whose extent references block device 1 must raise a clear
        SuperImageError instead of silently reading from the wrong location."""
        payload = b"SECONDARY" + b"\x00" * (LP_SECTOR_SIZE - 9)
        data = _build_super_image_raw(
            {"system_a": payload},
            num_block_devices=2,
            target_sources={"system_a": 1},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "super.img", data)
            metadata = read_super_metadata(path)
            self.assertEqual(metadata.block_device_count, 2)
            with self.assertRaisesRegex(
                SuperImageError, "block device 1.*only supports the primary block device"
            ):
                extract_partition(path, metadata, "system", "_a")


if __name__ == "__main__":
    unittest.main()
