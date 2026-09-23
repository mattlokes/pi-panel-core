"""Minimal asyncio Varlink client and server, shared by every pi-panel component."""

from .client import Client, Connection
from .errors import (
    EXPECTED_MORE,
    INTERFACE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMETER,
    METHOD_NOT_FOUND,
    ProtocolError,
    VarlinkError,
    VarlinkTimeout,
    VarlinkUnavailable,
)
from .notify import sd_notify
from .server import Broadcaster, Call, Interface, Server, SubscriberOverflow, Subscription

__all__ = [
    "Broadcaster",
    "Call",
    "Client",
    "Connection",
    "EXPECTED_MORE",
    "INTERFACE_NOT_FOUND",
    "INTERNAL_ERROR",
    "INVALID_PARAMETER",
    "Interface",
    "METHOD_NOT_FOUND",
    "ProtocolError",
    "Server",
    "SubscriberOverflow",
    "Subscription",
    "VarlinkError",
    "VarlinkTimeout",
    "VarlinkUnavailable",
    "sd_notify",
]

from .app import AppService  # noqa: E402
from .interfaces import load as load_interface  # noqa: E402

__all__ += ["AppService", "load_interface"]
