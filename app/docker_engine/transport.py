"""Transports and the raw-stream hijack helper for reaching a Docker Engine."""

import asyncio
import contextlib
import ipaddress
import os
import re
import secrets
import shutil
import socket
import ssl
import sys
import tempfile
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization

from app.net_transport import PolicyTransport
from app.netcheck import (
    BoundedResolver,
    connect_first,
    literal_address,
    usable_addresses,
)
from app.netpolicy import AddressRefused, ip_literal

DEFAULT_SOCKET_PATH = "/var/run/docker.sock"
DEFAULT_TLS_PORT = 2376
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_HANDSHAKE_TIMEOUT = 10.0

_UNIX_BASE_URL = "http://localhost"
_SWITCHING_PROTOCOLS = 101
_MAX_HEAD_BYTES = 64 * 1024
_MAX_REFUSAL_BODY_BYTES = 64 * 1024
# Longer than any length the refusal cap admits; keeps int() clear of its digit-count limit.
_MAX_LENGTH_DIGITS = 18
_TLS_WORKDIR_NAME = "tls-work"
_MULTIPLEXED_STREAM_TYPE = "application/vnd.docker.multiplexed-stream"

MAX_SOCKET_PATH_CHARS = 256
MAX_PORT = 65535

_MAX_HOSTNAME_CHARS = 253
_HOSTNAME_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
_SCHEME_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_URL_DELIMITERS = "?#"

HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

_tls_workdir_base: Path | None = None
_stream_resolver: BoundedResolver | None = None

PLAIN_TCP_REFUSAL = (
    "Refusing to connect to a Docker Engine over plain TCP. An unauthenticated Docker socket "
    "reachable over the network grants root on the host to anyone who can reach the port. "
    "Supply a CA certificate, client certificate and client key to connect with mutual TLS."
)


def has_hidden_characters(text: str) -> bool:
    """Report whether text holds a control, format or line-separator character.

    These render as nothing or as a different string, so two names containing them can look
    identical or hide what they say.
    """
    return any(unicodedata.category(c) in HIDDEN_CATEGORIES for c in text)


class HijackError(ConnectionError):
    """The Engine did not switch the connection to a raw stream."""

    def __init__(self, message: str, *, status: int | None = None, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """PEM text for mutual TLS; the client key arrives already decrypted.

    The PEM fields are excluded from equality because they are blanked once the context is built;
    anything that must notice changed material compares a fingerprint of it instead.
    """

    ca_cert_pem: str = field(compare=False)
    client_cert_pem: str = field(compare=False)
    client_key_pem: str = field(compare=False)
    _context: ssl.SSLContext | None = field(default=None, init=False, repr=False, compare=False)

    def __repr__(self) -> str:
        return "TlsMaterial(<redacted>)"

    def ssl_context(self) -> ssl.SSLContext:
        """Return the verifying client context, building it on first use.

        A successful build blanks the PEM fields so the decrypted key does not stay in the heap
        for as long as this object lives. That is a reduction, not an elimination: the copies made
        while decrypting and re-encrypting the key are not erased.
        """
        if self._context is None:
            object.__setattr__(self, "_context", _build_ssl_context(self))
            for name in ("ca_cert_pem", "client_cert_pem", "client_key_pem"):
                object.__setattr__(self, name, "")
        assert self._context is not None
        return self._context


def _resolver() -> BoundedResolver:
    global _stream_resolver
    if _stream_resolver is None:
        _stream_resolver = BoundedResolver()
    return _stream_resolver


class EndpointAddressRefused(ConnectionError):
    """The endpoint's address is internal and no permit covers it."""

    def __init__(self, refusal: AddressRefused) -> None:
        super().__init__(str(refusal))
        self.address = refusal.address


class _RefusedConnect(httpx.ConnectError):
    """A connection that was not made because the address policy refused the endpoint."""

    def __init__(self, refusal: AddressRefused, request: httpx.Request) -> None:
        super().__init__(str(refusal), request=request)
        self.address = refusal.address


class _EngineTransport(PolicyTransport):
    """Reports a refused address the way httpx reports any other connection that was not made."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await super().handle_async_request(request)
        except AddressRefused as refusal:
            raise _RefusedConnect(refusal, request) from None


@dataclass(frozen=True, slots=True)
class TransportBundle:
    """A transport and the base URL a client must use with it."""

    transport: httpx.AsyncBaseTransport
    base_url: str


class ConnectionSettings(ABC):
    """How to reach one Engine; each transport kind is one subclass."""

    @abstractmethod
    def create_transport(self) -> TransportBundle:
        """Build the httpx transport for this endpoint."""

    @abstractmethod
    async def open_stream(
        self, connect_timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a raw byte stream to this endpoint."""

    @property
    @abstractmethod
    def host_header(self) -> str:
        """Return the Host header value for requests to this endpoint."""


@dataclass(frozen=True, slots=True)
class UnixSocketSettings(ConnectionSettings):
    """A local Unix domain socket."""

    path: str = DEFAULT_SOCKET_PATH

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError("Unix socket path must be absolute")
        if ".." in self.path.split("/"):
            raise ValueError("Unix socket path must not contain '..'")

    def create_transport(self) -> TransportBundle:
        """Build a transport that dials the socket."""
        return TransportBundle(httpx.AsyncHTTPTransport(uds=self.path), _UNIX_BASE_URL)

    async def open_stream(
        self, connect_timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a raw stream to the socket."""
        if sys.platform == "win32":
            raise OSError("Unix sockets are not available on this platform")
        async with asyncio.timeout(connect_timeout):
            return await asyncio.open_unix_connection(self.path)

    @property
    def host_header(self) -> str:
        """Return the Host header value."""
        return "localhost"


@dataclass(frozen=True, slots=True)
class TcpSettings(ConnectionSettings):
    """A remote TCP endpoint; TLS material is mandatory."""

    host: str
    port: int
    tls: TlsMaterial | None

    def __post_init__(self) -> None:
        if self.tls is None:
            raise ValueError(PLAIN_TCP_REFUSAL)
        if not self.host or any(c in self.host for c in "/@ \t\r\n"):
            raise ValueError("TCP host is empty or malformed")
        if not 0 < self.port < 65536:
            raise ValueError("TCP port must be between 1 and 65535")

    @property
    def _authority(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    def _context(self) -> ssl.SSLContext:
        assert self.tls is not None
        return self.tls.ssl_context()

    def create_transport(self) -> TransportBundle:
        """Build a mutual-TLS transport."""
        transport = _EngineTransport(verify=self._context())
        return TransportBundle(transport, f"https://{self._authority}:{self.port}")

    async def open_stream(
        self, connect_timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a raw stream over verified TLS."""
        async with asyncio.timeout(connect_timeout):
            sock = await self._dial(connect_timeout)
            try:
                return await asyncio.open_connection(
                    sock=sock, ssl=self._context(), server_hostname=self.host
                )
            except BaseException:
                sock.close()
                raise

    async def _dial(self, connect_timeout: float) -> socket.socket:
        literal = ip_literal(self.host)
        if literal is not None:
            addresses = [literal_address(literal, self.port)]
        else:
            addresses = usable_addresses(await _resolver()(self.host, self.port))
        result = await connect_first(
            addresses, attempt_limit=connect_timeout, budget=lambda limit: (limit, False)
        )
        if result.sock is not None:
            return result.sock
        if result.kinds == ("address_refused",):
            raise EndpointAddressRefused(AddressRefused(str(addresses[0][1][0])))
        raise ConnectionError("the Engine's address could not be reached")

    @property
    def host_header(self) -> str:
        """Return the Host header value."""
        return f"{self._authority}:{self.port}"


def parse_endpoint(endpoint: str, tls: TlsMaterial | None = None) -> ConnectionSettings:
    """Turn a `unix://` or `tcp://` endpoint string into settings."""
    parts = urlsplit(endpoint)
    match parts.scheme:
        case "unix":
            # urlsplit puts a host in netloc; dropping it would silently aim a remote-looking
            # endpoint at the local socket.
            if parts.netloc:
                raise ValueError("a unix:// endpoint must not name a host; use unix:///path")
            return UnixSocketSettings(parts.path or DEFAULT_SOCKET_PATH)
        case "tcp":
            if parts.hostname is None:
                raise ValueError("tcp endpoint has no host")
            return TcpSettings(parts.hostname, parts.port or DEFAULT_TLS_PORT, tls)
        case _:
            raise ValueError("endpoint must start with unix:// or tcp://")


type EndpointField = Literal["socket_path", "host", "port"]


class EndpointError(ValueError):
    """An endpoint input was refused; `field` names the form field the message belongs to."""

    def __init__(self, field: EndpointField, message: str) -> None:
        super().__init__(message)
        self.field: EndpointField = field


def validate_socket_path(path: str) -> str:
    """Return a local socket path unchanged, or raise EndpointError."""
    if not path:
        raise EndpointError("socket_path", "Enter the path of the Docker socket.")
    if len(path) > MAX_SOCKET_PATH_CHARS:
        raise EndpointError(
            "socket_path", f"The socket path must be at most {MAX_SOCKET_PATH_CHARS} characters."
        )
    if not path.startswith("/"):
        raise EndpointError("socket_path", "The socket path must be absolute and start with '/'.")
    if ".." in path.split("/"):
        raise EndpointError("socket_path", "The socket path must not contain '..' segments.")
    if has_hidden_characters(path):
        raise EndpointError(
            "socket_path", "The socket path must not contain control or invisible characters."
        )
    if any(c in path for c in _URL_DELIMITERS):
        raise EndpointError("socket_path", "The socket path must not contain '?' or '#'.")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise EndpointError("socket_path", "The socket path is not valid text.") from None
    return path


def canonical_unix_endpoint(path: str) -> str:
    """Return `unix://<path>` for a validated socket path, proven to parse back to the same path."""
    url = f"unix://{validate_socket_path(path)}"
    parsed = parse_endpoint(url)
    if not isinstance(parsed, UnixSocketSettings) or parsed.path != path:
        raise EndpointError("socket_path", "The socket path cannot be written as an endpoint.")
    return url


def canonical_host(host: str) -> str:
    """Return the canonical spelling of a host, or raise EndpointError.

    Hostnames are IDNA-encoded and lower-cased, IPv4 is dotted-quad, IPv6 is compressed and
    unbracketed. Anything a resolver would read as a number in another notation is refused.
    """
    if not host:
        raise EndpointError("host", "Enter a host name or IP address.")
    scheme = _SCHEME_PREFIX.match(host)
    if scheme is not None:
        if scheme.group().lower() == "unix://":
            raise EndpointError(
                "host",
                "A unix:// address is a local socket. Choose the local socket kind and enter "
                "the path in the socket path field.",
            )
        raise EndpointError(
            "host",
            "Enter only the host name or IP address. The port has its own field and TLS is "
            "always used.",
        )
    if (
        any(c in host for c in "/@%\\")
        or any(c.isspace() for c in host)
        or has_hidden_characters(host)
    ):
        raise EndpointError(
            "host",
            "The host must not contain '/', '@', '%', spaces, control or invisible characters.",
        )

    bracketed = host.startswith("[") and host.endswith("]")
    bare = host[1:-1] if bracketed else host
    if ":" in bare:
        try:
            return ipaddress.IPv6Address(bare).compressed
        except ValueError:
            raise EndpointError(
                "host",
                "The host is not a valid IPv6 address. If it includes a port, enter the port "
                "in the port field.",
            ) from None
    if bracketed:
        raise EndpointError("host", "Square brackets are only valid around an IPv6 address.")
    try:
        return str(ipaddress.IPv4Address(bare))
    except ValueError:
        pass
    if _NUMERIC_LABEL.fullmatch(bare.rsplit(".", 1)[-1]):
        raise EndpointError("host", "The host is not a valid IPv4 address.")
    return _ascii_hostname(bare)


def _ascii_hostname(host: str) -> str:
    invalid = EndpointError("host", "The host is not a valid host name.")
    folded = host.lower()
    try:
        ascii_host = folded.encode("idna").decode("ascii").lower()
        rewritten = not folded.isascii() and ascii_host.encode("ascii").decode("idna") != folded
    except UnicodeError:
        raise invalid from None
    if rewritten:
        raise EndpointError(
            "host",
            "The host contains characters that name lookup would rewrite. Enter its ASCII or "
            "xn-- form.",
        )
    labels = ascii_host.split(".")
    if len(ascii_host) > _MAX_HOSTNAME_CHARS or not all(
        _HOSTNAME_LABEL.fullmatch(label) for label in labels
    ):
        raise invalid
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        raise EndpointError("host", "The host is not a valid IPv4 address.")
    return ascii_host


def canonical_tcp_endpoint(host: str, port: int) -> str:
    """Return `tcp://host:port` with IPv6 bracketed, proven to split back to the same pair."""
    normal = canonical_host(host)
    if isinstance(port, bool) or not 0 < port <= MAX_PORT:
        raise EndpointError("port", f"The port must be between 1 and {MAX_PORT}.")
    authority = f"[{normal}]" if ":" in normal else normal
    url = f"tcp://{authority}:{port}"
    try:
        round_trip = split_tcp_endpoint(url)
    except ValueError:
        raise EndpointError("host", "The host and port cannot be written as an endpoint.") from None
    if round_trip != (normal, port):
        raise EndpointError("host", "The host and port cannot be written as an endpoint.")
    return url


def split_tcp_endpoint(url: str) -> tuple[str, int]:
    """Return the unbracketed host and the port of a `tcp://` endpoint."""
    parts = urlsplit(url)
    if parts.scheme != "tcp" or parts.hostname is None:
        raise ValueError("not a tcp endpoint with a host")
    return parts.hostname, parts.port or DEFAULT_TLS_PORT


def configure_tls_workdir(data_dir: Path) -> Path:
    """Create the private base directory for client-identity files; call once at startup.

    Building a TLS context fails until this has run, so a hostile TMPDIR is never consulted.
    """
    global _tls_workdir_base
    base = data_dir / _TLS_WORKDIR_NAME
    if base.is_symlink():
        raise ValueError("TLS work directory must not be a symlink")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask and ignored for an existing directory.
    base.chmod(0o700)
    _tls_workdir_base = base.resolve()
    return _tls_workdir_base


def create_transport(settings: ConnectionSettings) -> TransportBundle:
    """Return a configured transport and the base URL to use with it."""
    return settings.create_transport()


def _build_ssl_context(tls: TlsMaterial) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    try:
        # Only the supplied CA is trusted; system roots are deliberately not loaded.
        context.load_verify_locations(cadata=tls.ca_cert_pem)
    except (ssl.SSLError, ValueError) as exc:
        # An empty CA string raises ValueError rather than SSLError.
        raise ValueError(f"CA certificate could not be loaded ({_reason(exc)})") from None
    _load_client_identity(context, tls)
    return context


def _reason(exc: Exception) -> str:
    return getattr(exc, "reason", None) or type(exc).__name__


def _load_client_identity(context: ssl.SSLContext, tls: TlsMaterial) -> None:
    # SSLContext.load_cert_chain accepts file paths only, so the identity must touch disk. The key
    # is re-encrypted under a throwaway passphrase first so no plaintext key is ever written, and
    # the directory is removed whether or not loading succeeds.
    passphrase = secrets.token_urlsafe(32)
    try:
        key = serialization.load_pem_private_key(tls.client_key_pem.encode("utf-8"), password=None)
        protected_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
        )
    except (ValueError, TypeError):
        raise ValueError("client key is not an unencrypted PEM private key") from None

    if _tls_workdir_base is None:
        raise RuntimeError("TLS work directory is not configured")
    workdir = Path(tempfile.mkdtemp(prefix="dockmin-tls-", dir=_tls_workdir_base))
    try:
        identity = workdir / "identity.pem"
        _write_private_file(identity, tls.client_cert_pem.encode("utf-8") + b"\n" + protected_key)
        try:
            context.load_cert_chain(identity, password=passphrase)
        except ssl.SSLError as exc:
            raise ValueError(
                f"client certificate and key could not be loaded ({_reason(exc)})"
            ) from None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _write_private_file(path: Path, data: bytes) -> None:
    # O_EXCL | O_NOFOLLOW: a pre-planted file or symlink at this name is an error, never a target.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(data)


class HijackedConnection:
    """A connection the Engine has switched to a raw bidirectional byte stream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        content_type: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.content_type = content_type

    @property
    def multiplexed(self) -> bool:
        """Report whether output is stdout/stderr frame-multiplexed rather than raw (a TTY)."""
        return self.content_type == _MULTIPLEXED_STREAM_TYPE

    async def read(self, size: int = 65536) -> bytes:
        """Read up to size bytes; an empty result means the peer closed the stream."""
        return await self._reader.read(size)

    async def readexactly(self, size: int) -> bytes:
        """Read exactly size bytes, raising IncompleteReadError if the stream ends first."""
        return await self._reader.readexactly(size)

    async def write(self, data: bytes) -> None:
        """Write data and wait until it has been flushed to the transport."""
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        """Close the stream; safe to call more than once."""
        self._writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await self._writer.wait_closed()

    async def __aenter__(self) -> "HijackedConnection":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def open_hijack(
    settings: ConnectionSettings,
    path: str,
    *,
    body: bytes = b"",
    content_type: str = "application/json",
    method: str = "POST",
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
) -> HijackedConnection:
    """Send an upgrade request and return the raw stream once the Engine answers 101.

    `path` is the full request target, including any version prefix and query string.
    """
    reader, writer = await settings.open_stream(connect_timeout)
    try:
        writer.write(_request_head(settings, method, path, body, content_type) + body)
        await writer.drain()
        async with asyncio.timeout(handshake_timeout):
            status, headers = await _read_response_head(reader)
            if status != _SWITCHING_PROTOCOLS:
                refusal = await _read_refusal_body(reader, headers)
                raise HijackError(
                    f"Engine did not upgrade the connection (HTTP {status})",
                    status=status,
                    body=refusal,
                )
    except BaseException:
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
        raise
    return HijackedConnection(reader, writer, headers.get("content-type", ""))


def _request_head(
    settings: ConnectionSettings, method: str, path: str, body: bytes, content_type: str
) -> bytes:
    for value in (method, path, content_type, settings.host_header):
        if any(c in value for c in "\r\n"):
            raise ValueError("request line and headers must not contain line breaks")
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {settings.host_header}",
        "Connection: Upgrade",
        "Upgrade: tcp",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body)}",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


async def _read_response_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        raise HijackError("Engine closed the connection before answering") from None
    except asyncio.LimitOverrunError:
        raise HijackError("Engine response header is too large") from None
    if len(raw) > _MAX_HEAD_BYTES:
        raise HijackError("Engine response header is too large")
    status_line, *header_lines = raw.decode("latin-1").split("\r\n")
    fields = status_line.split(" ", 2)
    if len(fields) < 2 or not fields[0].startswith("HTTP/") or not fields[1].isdigit():
        raise HijackError("Engine sent a malformed response")
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in header_lines if line)
    }
    return int(fields[1]), headers


async def _read_refusal_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    length = headers.get("content-length", "")
    if length.isascii() and length.isdigit():
        wanted = _MAX_REFUSAL_BODY_BYTES
        if len(length) <= _MAX_LENGTH_DIGITS:
            wanted = min(int(length), _MAX_REFUSAL_BODY_BYTES)
        with contextlib.suppress(asyncio.IncompleteReadError):
            return await reader.readexactly(wanted)
        return b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return await _read_chunked(reader)
    return await reader.read(_MAX_REFUSAL_BODY_BYTES)


async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
    body = b""
    try:
        while (remaining := _MAX_REFUSAL_BODY_BYTES - len(body)) > 0:
            size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
            # readexactly ignores the stream limit, so the declared size is bounded before the read.
            if not 0 < size <= remaining:
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ValueError):
        pass
    return body[:_MAX_REFUSAL_BODY_BYTES]
