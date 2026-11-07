"""Canonical endpoint spelling and the inputs it refuses, in app/docker_engine/transport.py."""

import pytest

from app.docker_engine.transport import (
    DEFAULT_TLS_PORT,
    MAX_SOCKET_PATH_CHARS,
    EndpointError,
    UnixSocketSettings,
    canonical_host,
    canonical_tcp_endpoint,
    canonical_unix_endpoint,
    parse_endpoint,
    split_tcp_endpoint,
    validate_socket_path,
)

ZERO_WIDTH_SPACE = "\u200b"
RIGHT_TO_LEFT_OVERRIDE = "\u202e"
LINE_SEPARATOR = "\u2028"


@pytest.mark.parametrize(
    ("host", "port", "endpoint"),
    [
        ("example.com", 2376, "tcp://example.com:2376"),
        ("Docker-Host.Example.COM", 2376, "tcp://docker-host.example.com:2376"),
        ("host_1", 2376, "tcp://host_1:2376"),
        ("10.0.0.5", 2376, "tcp://10.0.0.5:2376"),
        ("::1", 2376, "tcp://[::1]:2376"),
        ("[::1]", 2376, "tcp://[::1]:2376"),
        ("2001:DB8:0:0:0:0:0:1", 2376, "tcp://[2001:db8::1]:2376"),
        ("bücher.example", 2376, "tcp://xn--bcher-kva.example:2376"),
        ("xn--bcher-kva.example", 2376, "tcp://xn--bcher-kva.example:2376"),
        ("example.com", 65535, "tcp://example.com:65535"),
    ],
)
def test_an_accepted_host_and_port_are_written_canonically_and_split_back(
    host: str, port: int, endpoint: str
) -> None:
    written = canonical_tcp_endpoint(host, port)

    assert written == endpoint
    assert split_tcp_endpoint(written) == (canonical_host(host), port)


@pytest.mark.parametrize(
    "host",
    [
        "unix://host/var/run/docker.sock",
        "tcp://example.com",
        "https://example.com",
        "127.1",
        "0x7f.0.0.1",
        "0177.0.0.1",
        "2130706433",
        f"a{ZERO_WIDTH_SPACE}b.com",
        "ⓔⓧⓐⓜⓟⓛⓔ.com",
        "faß.de",
        "example.com:2376",
        "user@example.com",
        "example.com/path",
        "example.com\r\nX-Injected: 1",
        "example .com",
        "[10.0.0.5]",
        f"a{RIGHT_TO_LEFT_OVERRIDE}b.com",
        f"a{LINE_SEPARATOR}b.com",
        "example.com%00",
        "",
        "-leading.example",
    ],
)
def test_a_host_that_could_be_read_as_another_address_is_refused(host: str) -> None:
    with pytest.raises(EndpointError) as refused:
        canonical_tcp_endpoint(host, DEFAULT_TLS_PORT)

    assert refused.value.field == "host"


def test_a_unix_scheme_in_the_host_field_is_pointed_at_the_socket_kind() -> None:
    with pytest.raises(EndpointError, match="local socket kind"):
        canonical_host("unix://host/var/run/docker.sock")


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_a_port_outside_the_valid_range_is_refused(port: int) -> None:
    with pytest.raises(EndpointError) as refused:
        canonical_tcp_endpoint("example.com", port)

    assert refused.value.field == "port"


def test_a_valid_socket_path_is_written_as_a_unix_endpoint_that_parses_back() -> None:
    path = "/var/run/docker.sock"

    endpoint = canonical_unix_endpoint(path)

    assert endpoint == "unix:///var/run/docker.sock"
    assert parse_endpoint(endpoint) == UnixSocketSettings(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "docker.sock",
        "/run/../etc/docker.sock",
        "/run/docker.sock\n",
        f"/run/{RIGHT_TO_LEFT_OVERRIDE}docker.sock",
        f"/run/{ZERO_WIDTH_SPACE}docker.sock",
        "/run/docker.sock?x=1",
        "/run/docker.sock#x",
        "/" + "a" * MAX_SOCKET_PATH_CHARS,
    ],
    ids=[
        "empty",
        "relative",
        "parent segment",
        "line feed",
        "bidi override",
        "zero-width space",
        "query",
        "fragment",
        "too long",
    ],
)
def test_a_socket_path_that_is_not_plain_absolute_text_is_refused(path: str) -> None:
    with pytest.raises(EndpointError) as refused:
        validate_socket_path(path)

    assert refused.value.field == "socket_path"
