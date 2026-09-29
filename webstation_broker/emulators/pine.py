"""The PINE wire protocol, shared by PCSX2 and RPCS3.

Both emulators speak the same framing over a Unix socket; RPCS3 just
implements a smaller opcode set (no save/load-state). Each request opens its
own connection, so callers pass the socket path every time and a test can
point a module's `PINE_SOCKET` elsewhere.
"""

import logging
import socket as _socket
import struct
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

MAX_REPLY_BYTES = 64 * 1024
"""Largest reply the broker will read off a PINE socket.

The opcodes the broker sends answer in a handful of bytes. The declared size
is a u32 read straight off the wire, so without a ceiling a bogus one has the
broker buying 4 GiB of memory on the word of whatever is on the socket.
"""


def recv_exact(sock: _socket.socket, n: int, deadline: float, socket_path: Path) -> Optional[bytes]:
    """Read exactly `n` bytes from a PINE socket, on one shared deadline.

    Args:
        sock: A connected PINE socket.
        n: Number of bytes to read.
        deadline: `time.monotonic` value the whole read must finish by. A
            per-recv timeout alone never expires against a peer that dribbles
            one byte at a time, so the budget is spent, not restarted.
        socket_path: The socket's path, for the timeout warning.

    Returns:
        The bytes read, or None if the peer closed the connection or the deadline passed.
    """
    buf = b""
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("PINE read timed out with %d of %d bytes on %s", len(buf), n, socket_path)
            return None
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def request(socket_path: Path, opcode: int, payload: bytes = b"", timeout: float = 5.0) -> Optional[bytes]:
    """Send one PINE request and return the reply body.

    Wire format (little endian): u32 total size, u8 opcode, payload; the reply
    is u32 size, u8 result (0 = OK), payload.

    Args:
        socket_path: The emulator's PINE Unix socket.
        opcode: The PINE message opcode.
        payload: Bytes following the opcode.
        timeout: Seconds the whole exchange gets, connect through reply.

    Returns:
        The reply payload (possibly empty), or None when the socket is down, the peer hangs up,
        the reply declares a size outside `MAX_REPLY_BYTES`, or the emulator rejects the request
        with a non-zero result.
    """
    packet = struct.pack("<IB", 5 + len(payload), opcode) + payload
    deadline = time.monotonic() + timeout
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(packet)
            header = recv_exact(sock, 5, deadline, socket_path)
            if header is None:
                return None
            size, result = struct.unpack("<IB", header)
            if size < 5 or size > MAX_REPLY_BYTES:
                log.warning(
                    "PINE opcode 0x%02X declared an unusable reply of %d bytes on %s",
                    opcode,
                    size,
                    socket_path,
                )
                return None
            body = b""
            if size > 5:
                body = recv_exact(sock, size - 5, deadline, socket_path) or b""
            if result != 0:
                log.warning("PINE opcode 0x%02X rejected (result %d)", opcode, result)
                return None
            return body
    except OSError as exc:
        log.warning("PINE request failed on %s (opcode 0x%02X): %s", socket_path, opcode, exc)
        return None
