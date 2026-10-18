#!/usr/bin/env python3
"""Minimal host-side responder for the Cuttlefish sensors multihal handshake.

The vendor.sensors-hal-multihal HAL opens /dev/hvc18 as a control channel and
performs a blocking handshake at startup: it sends the literal request
"list-sensors" and expects a RawMessage-framed reply whose payload is an ASCII
decimal bitmask of host sensors. A reply of "0" (no host sensors) is a normal,
supported configuration and lets the HAL register its binder service.

This script takes one argument, the path to a UNIX domain socket, connects to
that socket, reads one RawMessage-framed request, writes the single-byte reply,
and then keeps the socket open forever so the HAL's control channel does not
see EOF.

The protocol framing follows device/google/cuttlefish/common/libs/transport/channel.h:

    struct RawMessage {
        uint32 command_with_response_bit;  // ignored by this responder
        uint32 payload_size;               // little-endian
        uint8  payload[payload_size];
    }
"""

from __future__ import annotations

import socket
import struct
import sys
import time


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly ``size`` bytes from ``sock`` or raise EOFError."""

    chunks: list[bytes] = []
    received = 0
    while received < size:
        chunk = sock.recv(size - received)
        if not chunk:
            raise EOFError(f"expected {size} bytes, got {received}")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _connect_with_retry(sock: socket.socket, path: str, timeout: float = 30.0) -> None:
    """Connect to ``path``, retrying until QEMU creates the listening socket."""

    deadline = time.time() + timeout
    while True:
        try:
            sock.connect(path)
            return
        except (FileNotFoundError, ConnectionRefusedError):
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    if len(args) != 2:
        print("usage: sensors_handshake_responder.py SOCKET_PATH", file=sys.stderr)
        return 2

    path = args[1]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _connect_with_retry(sock, path)

        # Read the 8-byte RawMessage header, then the declared payload.
        header = _recv_exact(sock, 8)
        payload_size = struct.unpack("<I", header[4:8])[0]
        _recv_exact(sock, payload_size)

        # Respond with a syntactically valid RawMessage whose payload is "0".
        # The command/is_response bitfield can be any value; the HAL accepts any.
        sock.sendall(struct.pack("<II", 0, 1) + b"0")

        # Keep the channel open so the HAL does not read EOF later.
        while True:
            time.sleep(3600)
    finally:
        sock.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
