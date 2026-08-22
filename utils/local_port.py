"""Deterministic port selection for local server startup."""

from __future__ import annotations

import socket
from collections.abc import Callable


DEFAULT_PORT = 5002
LOCAL_FALLBACK_PORT = 5003


class LocalPortsUnavailableError(RuntimeError):
    """Raised when both supported local ports are already occupied."""


def is_port_available(host: str, port: int) -> bool:
    """Return whether a TCP listener can bind to ``host:port``.

    Binding is the only reliable local check; connecting can report false
    negatives for listeners bound to a different interface.
    """

    bind_host = host.strip() or "0.0.0.0"
    try:
        addresses = socket.getaddrinfo(
            bind_host,
            port,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror:
        return False

    for family, socktype, protocol, _, sockaddr in addresses:
        try:
            with socket.socket(family, socktype, protocol) as candidate:
                candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                candidate.bind(sockaddr)
                return True
        except OSError:
            continue
    return False


def resolve_server_port(
    *,
    host: str,
    requested_port: int,
    stage: str,
    port_available: Callable[[str, int], bool] = is_port_available,
) -> int:
    """Resolve the bind port without changing non-local or explicit ports.

    Local startup has one compatibility fallback: the historical default
    ``5002`` moves to ``5003`` when occupied. Explicit ports other than 5002
    remain explicit and are left for Uvicorn to validate.
    """

    if stage.strip().lower() != "local" or requested_port != DEFAULT_PORT:
        return requested_port

    if port_available(host, DEFAULT_PORT):
        return DEFAULT_PORT
    if port_available(host, LOCAL_FALLBACK_PORT):
        return LOCAL_FALLBACK_PORT

    raise LocalPortsUnavailableError(
        "Local ports 5002 and 5003 are both in use. "
        "Set PORT to another available port and start again."
    )
