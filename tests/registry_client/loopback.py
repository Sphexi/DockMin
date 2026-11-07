"""Loopback TLS servers and certificates for the registry connection tests."""

import asyncio
import ipaddress
import socket
import ssl
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LOOPBACK = "127.0.0.1"
REGISTRY_NAME = "registry.test"
TOKEN_SERVICE_NAME = "auth.test"
ECR_NAME = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
OTHER_NAME = "elsewhere.test"
CRLF = b"\r\n"

_VALID_FROM = datetime(2020, 1, 1, tzinfo=UTC)
_VALID_UNTIL = datetime(2100, 1, 1, tzinfo=UTC)
_EXPIRED_AT = datetime(2021, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Identity:
    """A certificate chain and its key, both PEM."""

    cert_pem: str
    key_pem: str


@dataclass(frozen=True, slots=True)
class Certificates:
    """A trusted CA, an unrelated one, and the server identities the tests present."""

    ca_pem: str
    rogue_ca_pem: str
    server: Identity
    rogue_server: Identity
    wrong_name_server: Identity
    expired_server: Identity


def build_certificates() -> Certificates:
    """Generate the whole set in memory."""
    ca_key, ca = _authority("registry test CA")
    rogue_key, rogue = _authority("registry rogue CA")
    names = (REGISTRY_NAME, TOKEN_SERVICE_NAME, ECR_NAME)
    return Certificates(
        ca_pem=_pem(ca),
        rogue_ca_pem=_pem(rogue),
        server=_leaf(ca_key, ca, names, _VALID_UNTIL),
        rogue_server=_leaf(rogue_key, rogue, names, _VALID_UNTIL),
        wrong_name_server=_leaf(ca_key, ca, (OTHER_NAME,), _VALID_UNTIL, with_loopback=False),
        expired_server=_leaf(ca_key, ca, names, _EXPIRED_AT),
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _authority(common_name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(common_name)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_VALID_FROM)
        .not_valid_after(_VALID_UNTIL)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _leaf(
    ca_key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    names: tuple[str, ...],
    not_after: datetime,
    *,
    with_loopback: bool = True,
) -> Identity:
    key = ec.generate_private_key(ec.SECP256R1())
    alternatives: list[x509.GeneralName] = [x509.DNSName(name) for name in names]
    if with_loopback:
        alternatives.append(x509.IPAddress(ipaddress.ip_address(LOOPBACK)))
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(names[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_VALID_FROM)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return Identity(_pem(builder.sign(ca_key, hashes.SHA256())), key_pem)


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def server_context(identity: Identity) -> ssl.SSLContext:
    """Build a server context presenting the identity; load_cert_chain takes paths only."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "identity.pem"
        path.write_text(identity.cert_pem + identity.key_pem, encoding="utf-8")
        context.load_cert_chain(path)
    return context


@dataclass(frozen=True, slots=True)
class Request:
    """What a loopback server received."""

    method: str
    target: str
    headers: dict[str, str]

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True, slots=True)
class Stall:
    """Accept the request and never answer."""


@dataclass(frozen=True, slots=True)
class Hangup:
    """Close the connection without answering."""


type Answer = bytes | Stall | Hangup
type Handler = Callable[[Request], Answer]


def http_reply(
    status: int, body: bytes = b"", headers: dict[str, str] | None = None, reason: str = "Reply"
) -> bytes:
    """Build a complete HTTP/1.1 response that closes after the body."""
    fields = {"Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
    lines = [f"HTTP/1.1 {status} {reason}", *(f"{k}: {v}" for k, v in fields.items()), "", ""]
    return "\r\n".join(lines).encode("latin-1") + body


@dataclass(slots=True)
class LoopbackServer:
    """A listening socket on 127.0.0.1 that answers each request from a handler."""

    port: int
    requests: list[Request] = field(default_factory=list)
    _server: asyncio.Server | None = None
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)

    async def close(self) -> None:
        """Drop open connections and stop listening."""
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        await self._server.wait_closed()


async def serve(
    handler: Handler, *, identity: Identity | None = None, port: int = 0
) -> LoopbackServer:
    """Start a server; identity None serves plain TCP, which a TLS client cannot talk to."""
    context = None if identity is None else server_context(identity)
    holder = LoopbackServer(port=0)

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        holder._writers.add(writer)
        try:
            await _answer(handler, holder, reader, writer)
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            holder._writers.discard(writer)
            writer.transport.abort()

    server = await asyncio.start_server(connection, LOOPBACK, port, ssl=context)
    holder._server = server
    holder.port = server.sockets[0].getsockname()[1]
    return holder


async def _answer(
    handler: Handler,
    holder: LoopbackServer,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    head = await reader.readuntil(CRLF + CRLF)
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = {
        name.lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:] if line)
    }
    request = Request(method, target, headers)
    holder.requests.append(request)
    answer = handler(request)
    if isinstance(answer, Stall):
        await reader.read()
        return
    if isinstance(answer, Hangup):
        return
    writer.write(answer)
    await writer.drain()


async def serve_not_tls(reply: bytes | None) -> LoopbackServer:
    """Start a listener that answers a TLS hello in clear text, then closes."""
    holder = LoopbackServer(port=0)

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.transport.abort()

    server = await asyncio.start_server(connection, LOOPBACK, 0)
    holder._server = server
    holder.port = server.sockets[0].getsockname()[1]
    return holder


def closed_port() -> int:
    """Return a loopback port nothing listens on."""
    with socket.socket() as holder:
        holder.bind((LOOPBACK, 0))
        return int(holder.getsockname()[1])
