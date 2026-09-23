"""Exceptions shared by the client and the server."""

from __future__ import annotations

from typing import Any

from .protocol import ProtocolError

# Errors defined by the org.varlink.service interface itself.
INTERFACE_NOT_FOUND = "org.varlink.service.InterfaceNotFound"
METHOD_NOT_FOUND = "org.varlink.service.MethodNotFound"
METHOD_NOT_IMPLEMENTED = "org.varlink.service.MethodNotImplemented"
INVALID_PARAMETER = "org.varlink.service.InvalidParameter"
PERMISSION_DENIED = "org.varlink.service.PermissionDenied"
EXPECTED_MORE = "org.varlink.service.ExpectedMore"

# Not part of the varlink spec: reported when a handler raises something other
# than a VarlinkError. The message is included so the caller sees *why*.
INTERNAL_ERROR = "io.pipanel.InternalError"


class VarlinkError(Exception):
    """An error reply: a qualified error name plus its parameters.

    Raised by handlers on the server side (it becomes the error reply) and by
    the client when a call comes back with an error.
    """

    def __init__(self, error: str, parameters: dict[str, Any] | None = None) -> None:
        self.error = error
        self.parameters = parameters or {}
        detail = f" {self.parameters}" if self.parameters else ""
        super().__init__(f"{error}{detail}")

    @classmethod
    def invalid_parameter(cls, name: str) -> "VarlinkError":
        return cls(INVALID_PARAMETER, {"parameter": name})

    @classmethod
    def expected_more(cls) -> "VarlinkError":
        return cls(EXPECTED_MORE)


class VarlinkUnavailable(ConnectionError):
    """No server is listening, or it went away mid-call."""


class VarlinkTimeout(TimeoutError):
    """The server accepted the connection but did not answer in time."""


__all__ = [
    "INTERFACE_NOT_FOUND",
    "METHOD_NOT_FOUND",
    "METHOD_NOT_IMPLEMENTED",
    "INVALID_PARAMETER",
    "PERMISSION_DENIED",
    "EXPECTED_MORE",
    "INTERNAL_ERROR",
    "ProtocolError",
    "VarlinkError",
    "VarlinkUnavailable",
    "VarlinkTimeout",
]
