"""Tests for the sensors multihal hvc18 handshake responder script."""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SensorsHandshakeResponderTests(unittest.TestCase):
    def _script_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "src" / "sensors_handshake_responder.py"

    def test_replies_to_list_sensors_with_zero_mask(self) -> None:
        """The responder accepts a RawMessage-framed 'list-sensors' request
        and returns a valid RawMessage-framed reply with payload_size=1 and
        payload b'0'."""
        with tempfile.TemporaryDirectory() as raw:
            socket_path = Path(raw) / "test.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(self._script_path()), str(socket_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    conn, _ = server.accept()
                    with conn:
                        request = struct.pack("<II", 1, 12) + b"list-sensors"
                        conn.sendall(request)

                        header = conn.recv(8)
                        self.assertEqual(len(header), 8)
                        payload_size = struct.unpack("<I", header[4:8])[0]
                        self.assertEqual(payload_size, 1)
                        payload = conn.recv(payload_size)
                        self.assertEqual(payload, b"0")

                    # The responder must keep running so the HAL channel stays open.
                    self.assertIsNone(proc.poll())
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            finally:
                server.close()

    def test_command_bitfield_is_ignored(self) -> None:
        """Any value in the first 4 header bytes is accepted as a request."""
        with tempfile.TemporaryDirectory() as raw:
            socket_path = Path(raw) / "test.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(self._script_path()), str(socket_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    conn, _ = server.accept()
                    with conn:
                        # Use an all-zero command/is_response field.
                        request = struct.pack("<II", 0, 0)
                        conn.sendall(request)

                        header = conn.recv(8)
                        self.assertEqual(len(header), 8)
                        payload_size = struct.unpack("<I", header[4:8])[0]
                        self.assertEqual(payload_size, 1)
                        self.assertEqual(conn.recv(payload_size), b"0")
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            finally:
                server.close()


if __name__ == "__main__":
    unittest.main()
