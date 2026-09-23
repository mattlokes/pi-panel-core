"""Varlink wire format: JSON objects, each terminated by a single NUL byte.

    call   {"method": "iface.Method", "parameters": {...}, "more": true, "oneway": true}
    reply  {"parameters": {...}, "continues": true}
    error  {"error": "iface.ErrorName", "parameters": {...}}

See https://varlink.org/Method-Call for the full description.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# Upper bound on one message. Big enough for any list the panel will ever
# produce, small enough that a runaway peer cannot eat the Pi's memory.
MAX_MESSAGE = 4 * 1024 * 1024


class ProtocolError(Exception):
    """The peer sent something that is not a Varlink message."""


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\0"


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one message. Returns None on a clean EOF between messages."""
    try:
        raw = await reader.readuntil(b"\0")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError("connection closed in the middle of a message") from exc
    except asyncio.LimitOverrunError as exc:
        raise ProtocolError(f"message exceeds {MAX_MESSAGE} bytes") from exc
    try:
        message = json.loads(raw[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"message is not valid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message is not a JSON object")
    return message


def socket_path(address: str) -> str:
    """Accept both a bare path and varlink's `unix:` address form.

    `unix:@name` / `@name` select the Linux abstract namespace.
    """
    if address.startswith("unix:"):
        address = address[len("unix:"):]
    # A varlink address may carry ;mode=... style suffixes; we never use them.
    address = address.split(";", 1)[0]
    if address.startswith("@"):
        return "\0" + address[1:]
    return address
