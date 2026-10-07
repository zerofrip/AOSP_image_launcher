"""Unit tests for src/bootimg.py: native boot.img/vendor_boot.img parsing."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

from bootimg import (
    BootImageError,
    build_combined_initrd,
    read_boot_image,
    read_vendor_boot_image,
)

BOOT_MAGIC = b"ANDROID!"
VENDOR_BOOT_MAGIC = b"VNDRBOOT"


def _round_up(n: int, align: int) -> int:
    return ((n + align - 1) // align) * align


def _write(tmpdir: str, name: str, data: bytes) -> Path:
    path = Path(tmpdir) / name
    path.write_bytes(data)
    return path


def _make_v3v4_boot(
    *, header_version: int, kernel: bytes, ramdisk: bytes, cmdline: str = "", page_size: int = 4096
) -> bytes:
    header_len = 1584 if header_version == 4 else 1580
    header = bytearray(header_len)
    header[0:8] = BOOT_MAGIC
    struct.pack_into("<I", header, 8, len(kernel))
    struct.pack_into("<I", header, 12, len(ramdisk))
    struct.pack_into("<I", header, 40, header_version)
    cmdline_bytes = cmdline.encode("utf-8")[:1535]
    header[44:44 + len(cmdline_bytes)] = cmdline_bytes
    if header_version == 4:
        struct.pack_into("<I", header, 1580, 0)  # signature_size

    data = bytes(header)
    data += b"\x00" * (page_size - len(data))
    data += kernel
    data += b"\x00" * (_round_up(len(data), page_size) - len(data))
    data += ramdisk
    return data


def _make_v2_boot(*, kernel: bytes, ramdisk: bytes, cmdline: str = "", page_size: int = 2048) -> bytes:
    header = bytearray(1648)
    header[0:8] = BOOT_MAGIC
    struct.pack_into("<I", header, 8, len(kernel))   # kernel_size
    struct.pack_into("<I", header, 16, len(ramdisk))  # ramdisk_size
    struct.pack_into("<I", header, 36, page_size)     # page_size
    struct.pack_into("<I", header, 40, 2)              # header_version
    cmdline1 = cmdline.encode("utf-8")[:511]
    header[64:64 + len(cmdline1)] = cmdline1

    data = bytes(header)
    data += b"\x00" * (page_size - len(data))
    data += kernel
    data += b"\x00" * (_round_up(len(data), page_size) - len(data))
    data += ramdisk
    return data


def _make_vendor_boot(
    *,
    header_version: int,
    ramdisk: bytes,
    bootconfig: bytes = b"",
    cmdline: str = "",
    page_size: int = 4096,
    vrt_entry_num: int = 0,
    dtb: bytes = b"",
) -> bytes:
    header_len = 2128 if header_version == 4 else 2112
    header = bytearray(header_len)
    header[0:8] = VENDOR_BOOT_MAGIC
    struct.pack_into("<I", header, 8, header_version)
    struct.pack_into("<I", header, 12, page_size)
    struct.pack_into("<I", header, 24, len(ramdisk))
    cmdline_bytes = cmdline.encode("utf-8")[:2047]
    header[28:28 + len(cmdline_bytes)] = cmdline_bytes
    struct.pack_into("<I", header, 2096, header_len)  # header_size
    struct.pack_into("<I", header, 2100, len(dtb))     # dtb_size
    if header_version == 4:
        vrt_size = vrt_entry_num * 108 if vrt_entry_num else 0
        struct.pack_into("<I", header, 2112, vrt_size)
        struct.pack_into("<I", header, 2116, vrt_entry_num)
        struct.pack_into("<I", header, 2120, 108)
        struct.pack_into("<I", header, 2124, len(bootconfig))

    data = bytes(header)
    data += b"\x00" * (_round_up(2112, page_size) - len(data))
    data += ramdisk
    data += b"\x00" * (_round_up(len(data), page_size) - len(data))
    data += dtb
    if header_version == 4:
        vrt_size = vrt_entry_num * 108 if vrt_entry_num else 0
        data += b"\x00" * (_round_up(len(data), page_size) - len(data))
        data += b"\x00" * vrt_size
        if bootconfig:
            data += b"\x00" * (_round_up(len(data), page_size) - len(data))
            data += bootconfig
    return data


class ReadBootImageTests(unittest.TestCase):
    def test_v4_split_kernel_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"\x7fELFkernel", ramdisk=b""
            ))
            sec = read_boot_image(path)
            self.assertEqual(sec.header_version, 4)
            self.assertEqual(sec.kernel, b"\x7fELFkernel")
            self.assertEqual(sec.ramdisk, b"")

    def test_v4_init_boot_ramdisk_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b"cpio-ramdisk-data"
            ))
            sec = read_boot_image(path)
            self.assertEqual(sec.kernel, b"")
            self.assertEqual(sec.ramdisk, b"cpio-ramdisk-data")

    def test_v2_legacy_combined_kernel_and_ramdisk(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "boot.img", _make_v2_boot(
                kernel=b"KERNELBYTES", ramdisk=b"RAMDISKBYTES", cmdline="console=ttyS0"
            ))
            sec = read_boot_image(path)
            self.assertEqual(sec.header_version, 2)
            self.assertEqual(sec.kernel, b"KERNELBYTES")
            self.assertEqual(sec.ramdisk, b"RAMDISKBYTES")
            self.assertEqual(sec.cmdline, "console=ttyS0")

    def test_cmdline_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"K", ramdisk=b"", cmdline="androidboot.hardware=qemu"
            ))
            sec = read_boot_image(path)
            self.assertEqual(sec.cmdline, "androidboot.hardware=qemu")

    def test_bad_magic_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "boot.img", b"NOTABOOT" + b"\x00" * 100)
            with self.assertRaises(BootImageError):
                read_boot_image(path)

    def test_truncated_v4_header_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            # Enough to read magic + header_version=4 at offset 40, but far
            # short of the full 1584-byte v4 header.
            truncated = bytearray(44)
            truncated[0:8] = BOOT_MAGIC
            struct.pack_into("<I", truncated, 40, 4)
            path = _write(raw, "boot.img", bytes(truncated))
            with self.assertRaises(BootImageError) as ctx:
                read_boot_image(path)
            self.assertIn("truncated", str(ctx.exception).lower())

    def test_unsupported_header_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = bytearray(2000)
            data[0:8] = BOOT_MAGIC
            struct.pack_into("<I", data, 40, 99)
            path = _write(raw, "boot.img", bytes(data))
            with self.assertRaises(BootImageError):
                read_boot_image(path)

    def test_missing_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(BootImageError):
                read_boot_image(Path(raw) / "does-not-exist.img")


class ReadVendorBootImageTests(unittest.TestCase):
    def test_v3_no_bootconfig_no_vrt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "vendor_boot.img", _make_vendor_boot(
                header_version=3, ramdisk=b"VENDORRAMDISK", cmdline="androidboot.foo=bar"
            ))
            sec = read_vendor_boot_image(path)
            self.assertEqual(sec.header_version, 3)
            self.assertEqual(sec.ramdisk, b"VENDORRAMDISK")
            self.assertEqual(sec.bootconfig, b"")
            self.assertEqual(sec.cmdline, "androidboot.foo=bar")

    def test_v4_with_bootconfig_and_vrt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "vendor_boot.img", _make_vendor_boot(
                header_version=4,
                ramdisk=b"VENDORRAMDISKV4",
                bootconfig=b"androidboot.x=1\n\x0f\x00\x00\x00ABCD#BOOTCONFIG\n",
                cmdline="androidboot.hardware=cutef",
                vrt_entry_num=1,
            ))
            sec = read_vendor_boot_image(path)
            self.assertEqual(sec.header_version, 4)
            self.assertEqual(sec.ramdisk, b"VENDORRAMDISKV4")
            self.assertTrue(sec.bootconfig.endswith(b"#BOOTCONFIG\n"))
            self.assertEqual(sec.cmdline, "androidboot.hardware=cutef")

    def test_bad_magic_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = _write(raw, "vendor_boot.img", b"NOTVENDOR" + b"\x00" * 100)
            with self.assertRaises(BootImageError):
                read_vendor_boot_image(path)

    def test_truncated_v4_header_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            truncated = bytearray(16)
            truncated[0:8] = VENDOR_BOOT_MAGIC
            struct.pack_into("<I", truncated, 8, 4)
            path = _write(raw, "vendor_boot.img", bytes(truncated))
            with self.assertRaises(BootImageError) as ctx:
                read_vendor_boot_image(path)
            self.assertIn("truncated", str(ctx.exception).lower())

    def test_unsupported_header_version_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            data = bytearray(2200)
            data[0:8] = VENDOR_BOOT_MAGIC
            struct.pack_into("<I", data, 8, 99)
            path = _write(raw, "vendor_boot.img", bytes(data))
            with self.assertRaises(BootImageError):
                read_vendor_boot_image(path)


class BuildCombinedInitrdTests(unittest.TestCase):
    def test_combine_order_vendor_then_generic_then_bootconfig(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"KERNEL", ramdisk=b""
            ))
            init_boot_path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b"GENERIC_RAMDISK"
            ))
            vendor_boot_path = _write(raw, "vendor_boot.img", _make_vendor_boot(
                header_version=4, ramdisk=b"VENDOR_RAMDISK", bootconfig=b"BOOTCONFIG_TRAILER"
            ))
            boot_sec = read_boot_image(boot_path)
            init_sec = read_boot_image(init_boot_path)
            vend_sec = read_vendor_boot_image(vendor_boot_path)

            combined, cmdline = build_combined_initrd(boot_sec, init_sec, vend_sec)
            self.assertTrue(combined.startswith(b"VENDOR_RAMDISKGENERIC_RAMDISK"))
            self.assertTrue(combined.endswith(b"#BOOTCONFIG\n"))
            self.assertIn(b"BOOTCONFIG_TRAILER", combined)

    def test_no_vendor_boot_generic_ramdisk_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"KERNEL", ramdisk=b""
            ))
            init_boot_path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b"ONLY_GENERIC"
            ))
            boot_sec = read_boot_image(boot_path)
            init_sec = read_boot_image(init_boot_path)

            combined, cmdline = build_combined_initrd(boot_sec, init_sec, None)
            self.assertEqual(combined, b"ONLY_GENERIC")
            self.assertEqual(cmdline, "")

    def test_cmdline_join_vendor_then_boot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"K", ramdisk=b"R", cmdline="boot_cmdline_token"
            ))
            vendor_boot_path = _write(raw, "vendor_boot.img", _make_vendor_boot(
                header_version=4, ramdisk=b"VR", cmdline="vendor_cmdline_token"
            ))
            boot_sec = read_boot_image(boot_path)
            vend_sec = read_vendor_boot_image(vendor_boot_path)

            _, cmdline = build_combined_initrd(boot_sec, None, vend_sec)
            self.assertIn("vendor_cmdline_token", cmdline)
            self.assertIn("boot_cmdline_token", cmdline)
            self.assertLess(
                cmdline.index("vendor_cmdline_token"), cmdline.index("boot_cmdline_token")
            )

    def test_v3_combined_boot_as_fallback_when_no_init_boot(self) -> None:
        """Legacy v0-v3 images where boot.img carries the full generic ramdisk
        itself (no separate init_boot.img) must still work."""
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v2_boot(
                kernel=b"KERNEL", ramdisk=b"COMBINED_RAMDISK"
            ))
            boot_sec = read_boot_image(boot_path)
            combined, _cmdline = build_combined_initrd(boot_sec, None, None)
            self.assertEqual(combined, b"COMBINED_RAMDISK")

    def test_missing_kernel_raises(self) -> None:
        boot_sec = read_boot_image  # placeholder to appease linters; not used directly
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b""
            ))
            init_boot_path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b"R"
            ))
            boot_sec_v = read_boot_image(boot_path)
            init_sec_v = read_boot_image(init_boot_path)
            with self.assertRaises(BootImageError):
                build_combined_initrd(boot_sec_v, init_sec_v, None)

    def test_missing_ramdisk_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"K", ramdisk=b""
            ))
            boot_sec = read_boot_image(boot_path)
            with self.assertRaises(BootImageError):
                build_combined_initrd(boot_sec, None, None)


if __name__ == "__main__":
    unittest.main()


class CpioAndFstabTests(unittest.TestCase):
    def test_pack_cpio_newc_round_trips_with_cpio_tool_format(self) -> None:
        from bootimg import pack_cpio_newc
        data = pack_cpio_newc([("fstab.qemu", b"hello fstab\n")])
        self.assertTrue(data.startswith(b"070701"))
        self.assertIn(b"fstab.qemu\x00", data)
        self.assertIn(b"hello fstab\n", data)
        self.assertIn(b"TRAILER!!!", data)

    def test_pack_cpio_newc_multiple_entries(self) -> None:
        from bootimg import pack_cpio_newc
        data = pack_cpio_newc([("a.txt", b"AAA"), ("b.txt", b"BBBB")])
        self.assertIn(b"a.txt\x00", data)
        self.assertIn(b"b.txt\x00", data)
        self.assertIn(b"AAA", data)
        self.assertIn(b"BBBB", data)

    def test_build_partition_map_and_fstab_order_and_names(self) -> None:
        from bootimg import build_partition_map_and_fstab
        partition_map, fstab_name, fstab_text = build_partition_map_and_fstab(
            ["system", "vendor", "data"]
        )
        self.assertEqual(partition_map, "vda,system;vdb,vendor;vdc,data")
        self.assertEqual(fstab_name, "fstab.qemu")
        self.assertIn("/dev/block/by-name/system /system erofs", fstab_text)
        self.assertIn("/dev/block/by-name/vendor /vendor erofs", fstab_text)
        self.assertIn("/dev/block/by-name/data /data f2fs", fstab_text)
        # data must be read-write capable (no "ro" flag), others read-only
        for line in fstab_text.splitlines():
            if "/data " in line:
                self.assertNotIn(" ro ", f" {line} ")
            elif "/system " in line or "/vendor " in line:
                self.assertIn(" ro ", f" {line} ")

    def test_build_partition_map_custom_hardware_name(self) -> None:
        from bootimg import build_partition_map_and_fstab
        _pm, fstab_name, _text = build_partition_map_and_fstab(["system"], hardware="cf")
        self.assertEqual(fstab_name, "fstab.cf")

    def test_build_combined_initrd_splices_extra_ramdisk_into_generic_cpio(self) -> None:
        """extra_ramdisk must be spliced into the generic ramdisk's own cpio
        stream (before its trailer) rather than appended as a separate
        segment — appending a third independent segment is not reliably
        unpacked by the real kernel (verified by live tracing; see
        _splice_into_ramdisk_cpio's docstring), even though it looks
        correct in isolation."""
        from bootimg import pack_cpio_newc

        with tempfile.TemporaryDirectory() as raw:
            generic_cpio = pack_cpio_newc([("original_file", b"ORIGINAL_CONTENT")])
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"K", ramdisk=b""
            ))
            init_boot_path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=generic_cpio
            ))
            vendor_boot_path = _write(raw, "vendor_boot.img", _make_vendor_boot(
                header_version=4, ramdisk=b"VENDOR", bootconfig=b"BOOTCONFIGPARAMS"
            ))
            boot_sec = read_boot_image(boot_path)
            init_sec = read_boot_image(init_boot_path)
            vend_sec = read_vendor_boot_image(vendor_boot_path)

            extra_cpio = pack_cpio_newc([("injected_file", b"INJECTED_CONTENT")])
            combined, _cmdline = build_combined_initrd(
                boot_sec, init_sec, vend_sec, extra_ramdisk=extra_cpio
            )

            # the "#BOOTCONFIG\n" magic must remain the very last bytes
            self.assertTrue(combined.endswith(b"#BOOTCONFIG\n"))
            # vendor ramdisk is untouched, still the very first bytes
            self.assertTrue(combined.startswith(b"VENDOR"))
            # the spliced-in generic segment is one contiguous valid cpio
            # stream (not a separate appended archive) containing BOTH the
            # original generic file and the injected one
            self.assertIn(b"original_file", combined)
            self.assertIn(b"ORIGINAL_CONTENT", combined)
            self.assertIn(b"injected_file", combined)
            self.assertIn(b"INJECTED_CONTENT", combined)
            # injected content must appear strictly after the generic
            # ramdisk's own original content (spliced before its trailer,
            # not before it)
            self.assertLess(
                combined.index(b"ORIGINAL_CONTENT"), combined.index(b"INJECTED_CONTENT")
            )

    def test_build_combined_initrd_falls_back_gracefully_when_splice_impossible(self) -> None:
        """If the generic ramdisk isn't recognizable plain cpio or legacy
        LZ4 (e.g. a raw placeholder in a synthetic test fixture),
        build_combined_initrd must not raise — it just proceeds without
        the extra_ramdisk enhancement."""
        with tempfile.TemporaryDirectory() as raw:
            boot_path = _write(raw, "boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"K", ramdisk=b""
            ))
            init_boot_path = _write(raw, "init_boot.img", _make_v3v4_boot(
                header_version=4, kernel=b"", ramdisk=b"NOT_REAL_CPIO_OR_LZ4"
            ))
            boot_sec = read_boot_image(boot_path)
            init_sec = read_boot_image(init_boot_path)

            combined, _cmdline = build_combined_initrd(
                boot_sec, init_sec, None, extra_ramdisk=b"SOMETHING"
            )
            self.assertIn(b"NOT_REAL_CPIO_OR_LZ4", combined)
            self.assertNotIn(b"SOMETHING", combined)


class DetectHardwarePropertyTests(unittest.TestCase):
    def test_bootconfig_overrides_cmdline_and_default(self) -> None:
        from bootimg import VendorBootSections, detect_hardware_property
        vb = VendorBootSections(
            header_version=4, page_size=4096, ramdisk=b"",
            bootconfig=b"androidboot.hardware=cutf_cvm\nfoo=bar\n", cmdline="",
        )
        self.assertEqual(
            detect_hardware_property(vb, "androidboot.hardware=qemu"), "cutf_cvm"
        )

    def test_falls_back_to_cmdline_when_no_bootconfig_match(self) -> None:
        from bootimg import detect_hardware_property
        self.assertEqual(
            detect_hardware_property(None, "androidboot.hardware=qemu"), "qemu"
        )

    def test_falls_back_to_default_when_nothing_matches(self) -> None:
        from bootimg import detect_hardware_property
        self.assertEqual(detect_hardware_property(None, ""), "qemu")


class BootconfigTrailerTests(unittest.TestCase):
    def test_trailer_checksum_is_additive_byte_sum(self) -> None:
        from bootimg import _finish_bootconfig_trailer
        params = b"androidboot.hardware=qemu\n"
        result = _finish_bootconfig_trailer(params)
        self.assertTrue(result.startswith(params))
        self.assertTrue(result.endswith(b"#BOOTCONFIG\n"))
        trailer = result[len(params):]
        size, checksum = struct.unpack("<II", trailer[:8])
        self.assertEqual(size, len(params))
        self.assertEqual(checksum, sum(params) & 0xFFFFFFFF)
        self.assertEqual(trailer[8:], b"#BOOTCONFIG\n")


class SpliceIntoRamdiskCpioTests(unittest.TestCase):
    def test_splice_into_plain_cpio_before_trailer(self) -> None:
        from bootimg import pack_cpio_newc, _splice_into_ramdisk_cpio
        existing = pack_cpio_newc([("a.txt", b"AAA")])
        extra = pack_cpio_newc([("b.txt", b"BBB")])
        result = _splice_into_ramdisk_cpio(existing, extra)
        self.assertIn(b"a.txt", result)
        self.assertIn(b"AAA", result)
        self.assertIn(b"b.txt", result)
        self.assertIn(b"BBB", result)
        self.assertLess(result.index(b"AAA"), result.index(b"BBB"))
        # result must still be a single, valid, extractable cpio stream
        # (ends with exactly one TRAILER!!!, not two)
        self.assertEqual(result.count(b"TRAILER!!!"), 1)

    def test_splice_raises_on_unrecognized_format(self) -> None:
        from bootimg import _splice_into_ramdisk_cpio
        with self.assertRaises(BootImageError):
            _splice_into_ramdisk_cpio(b"totally not cpio or lz4", b"extra")

    def test_find_cpio_trailer_offset(self) -> None:
        from bootimg import pack_cpio_newc, _find_cpio_trailer_offset
        data = pack_cpio_newc([("x.txt", b"X")])
        offset = _find_cpio_trailer_offset(data)
        self.assertEqual(data[offset:offset + 6], b"070701")
        self.assertIn(b"TRAILER!!!", data[offset:])

    def test_looks_like_legacy_lz4(self) -> None:
        from bootimg import _looks_like_legacy_lz4
        self.assertTrue(_looks_like_legacy_lz4(b"\x02\x21\x4c\x18restofdata"))
        self.assertFalse(_looks_like_legacy_lz4(b"070701headerdata"))
        self.assertFalse(_looks_like_legacy_lz4(b""))


import shutil as _shutil


@unittest.skipUnless(_shutil.which("lz4"), "lz4 CLI tool not available")
class SpliceIntoLz4RamdiskCpioTests(unittest.TestCase):
    def test_splice_into_legacy_lz4_round_trips_via_lz4_tool(self) -> None:
        import subprocess as _subprocess
        from bootimg import pack_cpio_newc, _splice_into_ramdisk_cpio

        existing_plain = pack_cpio_newc([("orig.txt", b"ORIGDATA")])
        with tempfile.TemporaryDirectory() as raw:
            plain_path = Path(raw) / "plain"
            lz4_path = Path(raw) / "plain.lz4"
            plain_path.write_bytes(existing_plain)
            _subprocess.run(
                ["lz4", "-l", "-f", str(plain_path), str(lz4_path)],
                check=True, capture_output=True,
            )
            existing_lz4 = lz4_path.read_bytes()

        extra = pack_cpio_newc([("extra.txt", b"EXTRADATA")])
        result = _splice_into_ramdisk_cpio(existing_lz4, extra)

        # result must itself be legacy-LZ4-compressed again
        self.assertEqual(result[:4], b"\x02\x21\x4c\x18")

        with tempfile.TemporaryDirectory() as raw:
            result_lz4_path = Path(raw) / "result.lz4"
            result_plain_path = Path(raw) / "result"
            result_lz4_path.write_bytes(result)
            _subprocess.run(
                ["lz4", "-d", "-f", str(result_lz4_path), str(result_plain_path)],
                check=True, capture_output=True,
            )
            decompressed = result_plain_path.read_bytes()

        self.assertIn(b"orig.txt", decompressed)
        self.assertIn(b"ORIGDATA", decompressed)
        self.assertIn(b"extra.txt", decompressed)
        self.assertIn(b"EXTRADATA", decompressed)
        self.assertLess(decompressed.index(b"ORIGDATA"), decompressed.index(b"EXTRADATA"))
