"""A minimal HTTP/1.1 client for registry requests: one bounded GET or HEAD per connection."""

import asyncio
import re
import socket
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app import netpolicy

MAX_HEAD_BYTES: Final = 64 * 1024
MAX_BODY_BYTES: Final = 64 * 1024
MAX_HEADERS: Final = 100
MAX_INTERIM_RESPONSES: Final = 4
MAX_TRAILER_LINES: Final = 16

_STATUS_LINE = re.compile(r"^HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\x00-\x08\x0a-\x1f\x7f]*)?$")
_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_TARGET = re.compile(r"^/[\x21-\x7e]*$")
_HOST = re.compile(r"^[A-Za-z0-9._:\[\]-]+$")
_CHUNK_SIZE = re.compile(rb"^[0-9A-Fa-f]{1,8}$")
_NO_BODY_STATUSES = frozenset({204, 304})
_FIXED_HEADERS = frozenset(
    {"host", "connection", "accept-encoding", "content-length", "transfer-encoding"}
)
_OVERRIDABLE_HEADERS = frozenset({"user-agent", "accept"})


class WireFailure(StrEnum):
    """How an exchange failed."""

    CLOSED = "closed"
    MALFORMED = "malformed"
    OVERSIZED = "oversized"


class WireError(Exception):
    """A reply that could not be read; carries no peer text."""

    def __init__(self, failure: WireFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class WireResponse:
    """A status, header fields in order with lower-case names, and a bounded body."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        """Return the first value of a header, or None."""
        wanted = name.lower()
        return next((value for key, value in self.headers if key == wanted), None)

    def header_values(self, name: str) -> tuple[str, ...]:
        """Return every value of a header."""
        wanted = name.lower()
        return tuple(value for key, value in self.headers if key == wanted)


async def open_connection(
    family: socket.AddressFamily,
    sockaddr: tuple[Any, ...],
    *,
    server_name: str,
    context: ssl.SSLContext,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to one address and complete the TLS handshake, verifying against server_name.

    Raises netpolicy.AddressRefused, before any connection, for an address the policy refuses.
    """
    netpolicy.check_sockaddr(sockaddr)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, sockaddr)
        return await asyncio.open_connection(
            sock=sock, ssl=context, server_hostname=server_name, limit=MAX_HEAD_BYTES
        )
    except BaseException:
        sock.close()
        raise


async def get(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    target: str,
    host_header: str,
    headers: Mapping[str, str] | None = None,
    max_body: int | None = None,
) -> WireResponse:
    """Send a GET and read the reply; the body is read only when max_body is given.

    Raises WireError for an unreadable reply and ValueError if anything to be sent holds a
    character that could split the request.
    """
    return await _exchange("GET", reader, writer, target, host_header, headers or {}, max_body)


async def head(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    target: str,
    host_header: str,
    headers: Mapping[str, str] | None = None,
) -> WireResponse:
    """Send a HEAD and read the status line and headers; no body is ever read.

    Raises WireError for an unreadable reply and ValueError if anything to be sent holds a
    character that could split the request.
    """
    return await _exchange("HEAD", reader, writer, target, host_header, headers or {}, None)


async def _exchange(
    method: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target: str,
    host_header: str,
    headers: Mapping[str, str],
    max_body: int | None,
) -> WireResponse:
    writer.write(_request_bytes(method, target, host_header, headers))
    await writer.drain()
    for _ in range(MAX_INTERIM_RESPONSES):
        status, fields = await _read_head(reader)
        if status == 101:
            raise WireError(WireFailure.MALFORMED)
        if status >= 200:
            body = b""
            if method == "GET" and max_body is not None and status not in _NO_BODY_STATUSES:
                body = await _read_body(reader, fields, max_body)
            return WireResponse(status, fields, body)
    raise WireError(WireFailure.MALFORMED)


def _request_bytes(method: str, target: str, host_header: str, headers: Mapping[str, str]) -> bytes:
    if not _TARGET.match(target) or not _HOST.match(host_header):
        raise ValueError("request line holds characters that are not allowed")
    overrides: dict[str, str] = {}
    extra: list[str] = []
    for name, value in headers.items():
        if not _TOKEN.match(name) or not all(" " <= c <= "~" for c in value):
            raise ValueError("header holds characters that are not allowed")
        lowered = name.lower()
        if lowered in _FIXED_HEADERS:
            raise ValueError("header is set by the client")
        if lowered in _OVERRIDABLE_HEADERS:
            overrides[lowered] = f"{name}: {value}"
        else:
            extra.append(f"{name}: {value}")
    lines = [
        f"{method} {target} HTTP/1.1",
        f"Host: {host_header}",
        overrides.get("user-agent", "User-Agent: dockmin-registry-test"),
        overrides.get("accept", "Accept: application/json"),
        "Accept-Encoding: identity",
        "Connection: close",
        *extra,
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, tuple[tuple[str, str], ...]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        raise WireError(WireFailure.CLOSED) from None
    except asyncio.LimitOverrunError:
        raise WireError(WireFailure.OVERSIZED) from None
    lines = raw[:-4].decode("latin-1").split("\r\n")
    status_match = _STATUS_LINE.match(lines[0])
    if status_match is None or len(lines) - 1 > MAX_HEADERS:
        raise WireError(WireFailure.MALFORMED)
    fields: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        value = value.strip(" \t")
        if (
            not separator
            or not _TOKEN.match(name)
            or any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in value)
        ):
            raise WireError(WireFailure.MALFORMED)
        fields.append((name.lower(), value))
    return int(status_match.group(1)), tuple(fields)


async def _read_body(
    reader: asyncio.StreamReader, fields: Sequence[tuple[str, str]], limit: int
) -> bytes:
    values = {name: [v for n, v in fields if n == name] for name in {n for n, _ in fields}}
    if any(v.lower() != "identity" for v in values.get("content-encoding", [])):
        raise WireError(WireFailure.MALFORMED)
    encodings = [v.lower() for v in values.get("transfer-encoding", [])]
    lengths = set(values.get("content-length", []))
    if encodings and lengths:
        raise WireError(WireFailure.MALFORMED)
    try:
        if encodings:
            if encodings != ["chunked"]:
                raise WireError(WireFailure.MALFORMED)
            return await _read_chunked(reader, limit)
        if lengths:
            return await _read_counted(reader, lengths, limit)
        return await _read_to_end(reader, limit)
    except asyncio.IncompleteReadError:
        raise WireError(WireFailure.CLOSED) from None
    except asyncio.LimitOverrunError:
        raise WireError(WireFailure.MALFORMED) from None


async def _read_counted(reader: asyncio.StreamReader, lengths: set[str], limit: int) -> bytes:
    if len(lengths) != 1:
        raise WireError(WireFailure.MALFORMED)
    (text,) = lengths
    if not (text.isascii() and text.isdecimal()) or len(text) > 12:
        raise WireError(WireFailure.MALFORMED)
    if int(text) > limit:
        raise WireError(WireFailure.OVERSIZED)
    return await reader.readexactly(int(text))


async def _read_to_end(reader: asyncio.StreamReader, limit: int) -> bytes:
    data = await reader.read(limit + 1)
    while len(data) <= limit:
        more = await reader.read(limit + 1 - len(data))
        if not more:
            return data
        data += more
    raise WireError(WireFailure.OVERSIZED)


async def _read_chunked(reader: asyncio.StreamReader, limit: int) -> bytes:
    body = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size_text = line[:-2].split(b";", 1)[0].strip()
        if not _CHUNK_SIZE.match(size_text):
            raise WireError(WireFailure.MALFORMED)
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > limit:
            raise WireError(WireFailure.OVERSIZED)
        body += await reader.readexactly(size)
        if await reader.readexactly(2) != b"\r\n":
            raise WireError(WireFailure.MALFORMED)
    for _ in range(MAX_TRAILER_LINES):
        if await reader.readuntil(b"\r\n") == b"\r\n":
            return bytes(body)
    raise WireError(WireFailure.MALFORMED)
