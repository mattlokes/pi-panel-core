"""sd_notify(3) without libsystemd: one datagram to $NOTIFY_SOCKET.

Used by Type=notify units to say "ready" once their sockets are bound, so
anything ordered After= them starts only when it can actually connect.
"""

from __future__ import annotations

import os
import socket


def sd_notify(state: str = "READY=1") -> bool:
    """Returns False when not running under systemd (or on any error)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
        return True
    except OSError:
        return False
