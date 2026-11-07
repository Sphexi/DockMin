"""Tests for settings validation, TLS contexts and the hijack helper in transport.py."""

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.docker_engine.transport import (
    DEFAULT_SOCKET_PATH,
    DEFAULT_TLS_PORT,
    ConnectionSettings,
    HijackError,
    TcpSettings,
    TlsMaterial,
    TransportBundle,
    UnixSocketSettings,
    create_transport,
    open_hijack,
    parse_endpoint,
)
from tests.docker_engine.fake_engine import BODY_TOKEN
from tests.docker_engine.pki import LOOPBACK, Pki

PLACEHOLDER_TLS = TlsMaterial("ca", "cert", "key")
REFUSAL_BODY_CAP = 64 * 1024
HUGE_CHUNK_SIZE = b"FFFFFFFFFFFFFFFF"
OVER_LONG_LENGTH = b"9" * 5000
NON_ASCII_DIGIT_LENGTH = "\u00b2".encode("latin-1")
HOST = "engine.test"
PORT = 2376
ATTACH_PATH = "/v1.51/containers/abc123/attach?stream=1"
MULTIPLEXED = "application/vnd.docker.multiplexed-stream"
SWITCHED = (
    b"HTTP/1.1 101 UPGRADED\r\nContent-Type: %s\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
)


def test_plain_tcp_is_refused_with_an_explanation() -> None:
    with pytest.raises(ValueError, match=r"plain TCP.*mutual TLS"):
        TcpSettings(HOST, PORT, None)


def test_a_tcp_endpoint_string_without_tls_material_is_refused() -> None:
    with pytest.raises(ValueError, match="plain TCP"):
        parse_endpoint(f"tcp://{HOST}:2375")


@pytest.mark.parametrize(
    ("host", "port"),
    [("", PORT), ("a b", PORT), ("host/path", PORT), ("user@host", PORT), (HOST, 0), (HOST, 65536)],
)
def test_a_malformed_tcp_host_or_port_is_refused(host: str, port: int) -> None:
    with pytest.raises(ValueError):
        TcpSettings(host, port, PLACEHOLDER_TLS)


@pytest.mark.parametrize(
    "endpoint", ["http://host", "npipe:////./pipe/docker_engine", "host:2376", ""]
)
def test_an_endpoint_with_an_unsupported_scheme_is_refused(endpoint: str) -> None:
    with pytest.raises(ValueError, match="unix:// or tcp://"):
        parse_endpoint(endpoint, PLACEHOLDER_TLS)


@pytest.mark.parametrize(
    "endpoint",
    [
        "unix://host/var/run/docker.sock",
        "unix://remote.example",
        "unix://host:2375/run/docker.sock",
    ],
)
def test_a_unix_endpoint_that_names_a_host_is_refused(endpoint: str) -> None:
    with pytest.raises(ValueError, match="must not name a host"):
        parse_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint", ["unix:///run/../etc/docker.sock", "unix:///run/docker.sock/.."]
)
def test_a_unix_socket_path_with_a_parent_directory_segment_is_refused(endpoint: str) -> None:
    with pytest.raises(ValueError, match=r"'\.\.'"):
        parse_endpoint(endpoint)


def test_a_relative_socket_path_is_refused() -> None:
    with pytest.raises(ValueError, match="absolute"):
        UnixSocketSettings("docker.sock")


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("unix:///run/docker.sock", UnixSocketSettings("/run/docker.sock")),
        ("unix:///var/run/docker.sock", UnixSocketSettings("/var/run/docker.sock")),
        ("unix:///run/docker..sock", UnixSocketSettings("/run/docker..sock")),
        ("unix://", UnixSocketSettings(DEFAULT_SOCKET_PATH)),
        (f"tcp://{HOST}", TcpSettings(HOST, DEFAULT_TLS_PORT, PLACEHOLDER_TLS)),
        (f"tcp://{HOST}:2377", TcpSettings(HOST, 2377, PLACEHOLDER_TLS)),
    ],
)
def test_endpoint_strings_parse_to_settings(endpoint: str, expected: ConnectionSettings) -> None:
    assert parse_endpoint(endpoint, PLACEHOLDER_TLS) == expected


def test_a_unix_socket_gets_a_uds_transport_and_a_loopback_base_url() -> None:
    bundle = create_transport(UnixSocketSettings("/run/docker.sock"))

    assert isinstance(bundle.transport, httpx.AsyncHTTPTransport)
    assert bundle.base_url == "http://localhost"


@pytest.mark.parametrize(
    ("host", "base_url", "host_header"),
    [
        (LOOPBACK, f"https://{LOOPBACK}:{PORT}", f"{LOOPBACK}:{PORT}"),
        ("::1", f"https://[::1]:{PORT}", f"[::1]:{PORT}"),
    ],
)
def test_a_tcp_endpoint_gets_an_https_base_url_with_ipv6_hosts_bracketed(
    tls_material: TlsMaterial, host: str, base_url: str, host_header: str
) -> None:
    settings = TcpSettings(host, PORT, tls_material)

    bundle = settings.create_transport()

    assert bundle.base_url == base_url
    assert settings.host_header == host_header


def test_the_client_context_requires_a_verified_server_and_trusts_only_the_supplied_ca(
    tls_material: TlsMaterial,
) -> None:
    context = tls_material.ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.cert_store_stats()["x509_ca"] == 1


def test_the_context_is_built_once_and_reused(tls_material: TlsMaterial) -> None:
    first = tls_material.ssl_context()

    assert tls_material.ssl_context() is first


def test_tls_material_repr_shows_none_of_its_contents(tls_material: TlsMaterial) -> None:
    assert repr(tls_material) == "TlsMaterial(<redacted>)"


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("bad_ca", "CA certificate could not be loaded"),
        ("empty_ca", "CA certificate could not be loaded"),
        ("garbage_key", "not an unencrypted PEM private key"),
        ("mismatched_key", "client certificate and key could not be loaded"),
    ],
)
def test_unusable_tls_material_is_a_value_error_naming_the_part_that_failed(
    pki: Pki, variant: str, message: str
) -> None:
    variants = {
        "bad_ca": TlsMaterial("not a certificate", pki.client.cert_pem, pki.client.key_pem),
        "empty_ca": TlsMaterial("", pki.client.cert_pem, pki.client.key_pem),
        "garbage_key": TlsMaterial(pki.ca_pem, pki.client.cert_pem, "not a key"),
        "mismatched_key": TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.server.key_pem),
    }

    with pytest.raises(ValueError, match=message):
        variants[variant].ssl_context()


@dataclass
class FakeWriter:
    """Stands in for a StreamWriter, recording what is written and whether it was closed."""

    written: bytearray = field(default_factory=bytearray)
    closed: bool = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@dataclass(frozen=True)
class ScriptedSettings(ConnectionSettings):
    """Settings whose stream is an in-memory reader fed by the test."""

    reader: asyncio.StreamReader
    writer: FakeWriter
    host: str = HOST

    def create_transport(self) -> TransportBundle:
        raise NotImplementedError

    async def open_stream(self, connect_timeout: float) -> tuple[Any, Any]:
        return self.reader, self.writer

    @property
    def host_header(self) -> str:
        return self.host


class RecordingReader(asyncio.StreamReader):
    """A reader that records the size of every readexactly request."""

    def __init__(self) -> None:
        super().__init__()
        self.exact_sizes: list[int] = []

    async def readexactly(self, n: int) -> bytes:
        self.exact_sizes.append(n)
        return await super().readexactly(n)


def _scripted(
    response: bytes, *, host: str = HOST, reader: asyncio.StreamReader | None = None
) -> tuple[ScriptedSettings, FakeWriter]:
    reader = reader or asyncio.StreamReader()
    reader.feed_data(response)
    reader.feed_eof()
    writer = FakeWriter()
    return ScriptedSettings(reader, writer, host), writer


@pytest.mark.parametrize(
    ("content_type", "multiplexed"),
    [(MULTIPLEXED, True), ("application/vnd.docker.raw-stream", False)],
)
async def test_hijack_returns_the_raw_stream_after_a_switching_protocols_answer(
    content_type: str, multiplexed: bool
) -> None:
    settings, writer = _scripted((SWITCHED % content_type.encode()) + b"payload")

    connection = await open_hijack(settings, ATTACH_PATH, body=b"{}")

    assert connection.multiplexed is multiplexed
    assert await connection.readexactly(7) == b"payload"
    assert bytes(writer.written).startswith(b"POST " + ATTACH_PATH.encode() + b" HTTP/1.1\r\n")
    assert b"Upgrade: tcp\r\n" in writer.written
    assert f"Host: {HOST}\r\n".encode() in writer.written
    assert writer.written.endswith(b"\r\n\r\n{}")


async def test_hijack_refusal_reports_the_status_and_keeps_the_body_out_of_the_message() -> None:
    body = BODY_TOKEN.encode()
    settings, writer = _scripted(
        b"HTTP/1.1 409 Conflict\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)
    )

    with pytest.raises(HijackError) as raised:
        await open_hijack(settings, ATTACH_PATH)

    assert raised.value.status == 409
    assert raised.value.body == body
    assert BODY_TOKEN not in str(raised.value)
    assert writer.closed


@pytest.mark.parametrize(
    "response",
    [b"", b"HTTP/1.1 101", b"garbage\r\n\r\n", b"HTTP/1.1 abc OK\r\n\r\n"],
    ids=["closed-silently", "closed-mid-header", "not-http", "non-numeric-status"],
)
async def test_hijack_against_an_unusable_answer_raises_and_closes_the_stream(
    response: bytes,
) -> None:
    settings, writer = _scripted(response)

    with pytest.raises(HijackError):
        await open_hijack(settings, ATTACH_PATH)

    assert writer.closed


@pytest.mark.parametrize("path", ["/x\r\nHost: evil", "/x\nInjected: 1"])
async def test_hijack_refuses_a_request_target_containing_line_breaks_and_writes_nothing(
    path: str,
) -> None:
    settings, writer = _scripted(b"")

    with pytest.raises(ValueError, match="line breaks"):
        await open_hijack(settings, path)

    assert bytes(writer.written) == b""
    assert writer.closed


async def test_a_chunked_refusal_declaring_a_huge_chunk_reads_none_of_it_and_returns_no_body() -> (
    None
):
    reader = RecordingReader()
    settings, _ = _scripted(
        b"HTTP/1.1 409 Conflict\r\nTransfer-Encoding: chunked\r\n\r\n"
        + HUGE_CHUNK_SIZE
        + b"\r\nsome bytes",
        reader=reader,
    )

    with pytest.raises(HijackError) as raised:
        await open_hijack(settings, ATTACH_PATH)

    assert raised.value.body == b""
    assert max(reader.exact_sizes, default=0) <= REFUSAL_BODY_CAP


async def test_a_chunked_refusal_with_ordinary_chunks_returns_the_body() -> None:
    settings, _ = _scripted(
        b"HTTP/1.1 409 Conflict\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    )

    with pytest.raises(HijackError) as raised:
        await open_hijack(settings, ATTACH_PATH)

    assert raised.value.body == b"hello"


@pytest.mark.parametrize(
    "length",
    [
        pytest.param(OVER_LONG_LENGTH, id="over-long"),
        pytest.param(NON_ASCII_DIGIT_LENGTH, id="non-ascii-digit"),
    ],
)
async def test_a_refusal_with_an_over_long_or_non_ascii_content_length_is_a_hijack_error(
    length: bytes,
) -> None:
    settings, writer = _scripted(
        b"HTTP/1.1 409 Conflict\r\nContent-Length: " + length + b"\r\n\r\nbody"
    )

    with pytest.raises(HijackError) as raised:
        await open_hijack(settings, ATTACH_PATH)

    assert raised.value.status == 409
    assert writer.closed


@pytest.mark.parametrize("host", ["engine.test\r\nX-Injected: 1", "engine.test\nX-Injected: 1"])
async def test_hijack_refuses_a_host_header_containing_line_breaks_and_writes_nothing(
    host: str,
) -> None:
    settings, writer = _scripted(b"", host=host)

    with pytest.raises(ValueError, match="line breaks"):
        await open_hijack(settings, ATTACH_PATH)

    assert bytes(writer.written) == b""
    assert writer.closed
