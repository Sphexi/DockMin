"""ASGI application for the plain listener in redirect mode."""

import ipaddress
import re
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from app.web.security_headers import SecurityHeadersMiddleware

REDIRECT_METHODS: Final = frozenset({"GET", "HEAD"})
MAX_HOST_LENGTH: Final = 253
MAX_PORT_SUFFIX: Final = 6
MAX_PORT: Final = 65535
DEFAULT_HTTPS_PORT: Final = 443
WEBSOCKET_POLICY_VIOLATION: Final = 1008
REDIRECT_LISTENER: Final = "redirect"

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DNS_HOST = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}\.?")
_IPV6_HOST = re.compile(r"\[([0-9A-Fa-f:.]{2,45})\]")
_PORT = re.compile(r"[0-9]{1,5}")
_TARGET = re.compile(rb"[\x21-\x7e\x80-\xff]*")


class HttpsRedirectApp:
    """Answer GET and HEAD with a 308 to HTTPS and refuse everything else."""

    def __init__(self, *, public_port: int, trusted_origins: Sequence[str]) -> None:
        self._public_port = public_port
        self._allowed_hosts = frozenset(
            host
            for origin in trusted_origins
            if (parts := urlsplit(origin)).scheme == "https" and (host := parts.hostname)
        )
        self._app: ASGIApp = SecurityHeadersMiddleware(self._handle)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one connection through the security headers."""
        await self._app(scope, receive, send)

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": WEBSOCKET_POLICY_VIOLATION})
        elif scope["type"] == "http":
            await self._respond(scope, send)

    async def _respond(self, scope: Scope, send: Send) -> None:
        if scope["method"] not in REDIRECT_METHODS:
            await _refuse(send, b"Use HTTPS\n")
            return
        location = self._location(scope)
        if location is None:
            await _refuse(send, b"Invalid request\n")
            return
        await send(
            {
                "type": "http.response.start",
                "status": 308,
                "headers": [
                    (b"location", location),
                    (b"cache-control", b"no-store"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    def _location(self, scope: Scope) -> bytes | None:
        host = self._host(scope)
        target: bytes = scope.get("raw_path") or scope["path"].encode("utf-8")
        query: bytes = scope.get("query_string", b"")
        if (
            host is None
            or not target.startswith(b"/")
            or not _TARGET.fullmatch(target)
            or not _TARGET.fullmatch(query)
        ):
            return None
        authority = (
            host if self._public_port == DEFAULT_HTTPS_PORT else f"{host}:{self._public_port}"
        )
        location = b"https://" + authority.encode("ascii") + target
        return location + b"?" + query if query else location

    def _host(self, scope: Scope) -> str | None:
        values = [value for name, value in scope["headers"] if name == b"host"]
        if len(values) != 1 or len(values[0]) > MAX_HOST_LENGTH + MAX_PORT_SUFFIX:
            return None
        try:
            authority = _split_authority(values[0].decode("ascii"))
        except UnicodeDecodeError:
            return None
        if authority is None:
            return None
        host, port = authority
        if (port is not None and int(port) > MAX_PORT) or len(host) > MAX_HOST_LENGTH:
            return None
        if not _valid_host(host):
            return None
        bare = host.removeprefix("[").removesuffix("]").rstrip(".").lower()
        if self._allowed_hosts and bare not in self._allowed_hosts:
            return None
        return host.lower()


def _split_authority(raw: str) -> tuple[str, str | None] | None:
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return None
        host, rest = raw[: end + 1], raw[end + 1 :]
    else:
        host, separator, port = raw.partition(":")
        rest = separator + port
    if not rest:
        return host, None
    if rest[0] != ":" or not _PORT.fullmatch(rest[1:]):
        return None
    return host, rest[1:]


def _valid_host(host: str) -> bool:
    if host.startswith("["):
        match = _IPV6_HOST.fullmatch(host)
        if match is None:
            return False
        try:
            ipaddress.IPv6Address(match[1])
        except ValueError:
            return False
        return True
    return _DNS_HOST.fullmatch(host) is not None


async def _refuse(send: Send, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 400,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
