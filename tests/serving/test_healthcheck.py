"""Tests for the container health probe."""

import http.server
import ssl
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path

import pytest

from app.config import TLS_SELF_SIGNED_VAR, Settings, load_settings
from app.paths import HEALTH_PATH
from app.serving import healthcheck
from tests.tls_support import write_server_files

HEALTHY = 0
UNHEALTHY = 1
LOOPBACK = "127.0.0.1"
WILDCARD = {"0.0.0.0": "127.0.0.1", "::": "::1"}
PLAIN = {TLS_SELF_SIGNED_VAR: "false"}


def make_handler(status: HTTPStatus, seen: list[str]) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    return Handler


@contextmanager
def stub_server(status: HTTPStatus, seen: list[str], pair: object | None = None) -> Iterator[int]:
    server = http.server.HTTPServer((LOOPBACK, 0), make_handler(status, seen))
    if pair is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pair.cert, pair.key)  # type: ignore[attr-defined]
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    for name in (
        "DOCKMIN_HOST",
        "DOCKMIN_PORT",
        "DOCKMIN_HTTPS_PORT",
        "DOCKMIN_TLS_CERT_FILE",
        "DOCKMIN_TLS_KEY_FILE",
        TLS_SELF_SIGNED_VAR,
    ):
        monkeypatch.delenv(name, raising=False)

    def apply(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return apply


def test_the_probe_defaults_are_the_settings_defaults() -> None:
    fields = Settings.model_fields

    assert str(fields["port"].default) == healthcheck.DEFAULT_PORT
    assert str(fields["https_port"].default) == healthcheck.DEFAULT_HTTPS_PORT
    assert fields["host"].default in healthcheck.LOOPBACK_FOR_WILDCARD


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        pytest.param({}, (LOOPBACK, 8443, True), id="defaults-are-self-signed"),
        pytest.param({TLS_SELF_SIGNED_VAR: ""}, (LOOPBACK, 8443, True), id="blank-is-self-signed"),
        pytest.param(
            {"DOCKMIN_HTTPS_PORT": "9443"}, (LOOPBACK, 9443, True), id="self-signed-https-port"
        ),
        pytest.param(
            {"DOCKMIN_PORT": "9000"}, (LOOPBACK, 8443, True), id="self-signed-ignores-the-http-port"
        ),
        pytest.param(PLAIN, (LOOPBACK, 8000, False), id="plain-defaults"),
        pytest.param(PLAIN | {"DOCKMIN_PORT": "9000"}, (LOOPBACK, 9000, False), id="plain-port"),
        pytest.param(PLAIN | {"DOCKMIN_HOST": "::"}, ("::1", 8000, False), id="v6-wildcard"),
        pytest.param(
            PLAIN | {"DOCKMIN_HOST": "192.0.2.4"}, ("192.0.2.4", 8000, False), id="named-host"
        ),
        pytest.param(
            {"DOCKMIN_TLS_CERT_FILE": "/c", "DOCKMIN_TLS_KEY_FILE": "/k"},
            (LOOPBACK, 8443, True),
            id="tls-default-port",
        ),
        pytest.param(
            PLAIN | {"DOCKMIN_TLS_CERT_FILE": "/c", "DOCKMIN_TLS_KEY_FILE": "/k"},
            (LOOPBACK, 8443, True),
            id="supplied-files-win-over-self-signed-off",
        ),
        pytest.param(
            {
                "DOCKMIN_TLS_CERT_FILE": "/c",
                "DOCKMIN_TLS_KEY_FILE": "/k",
                "DOCKMIN_HTTPS_PORT": "9443",
                "DOCKMIN_PORT": "9000",
            },
            (LOOPBACK, 9443, True),
            id="tls-uses-the-https-port",
        ),
        pytest.param(
            PLAIN | {"DOCKMIN_TLS_CERT_FILE": "/c"},
            (LOOPBACK, 8000, False),
            id="half-a-pair-is-plain",
        ),
        pytest.param(
            PLAIN | {"DOCKMIN_TLS_CERT_FILE": " ", "DOCKMIN_TLS_KEY_FILE": " "},
            (LOOPBACK, 8000, False),
            id="blank-pair-is-plain",
        ),
    ],
)
def test_the_probe_picks_the_listener_the_process_serves_with(
    environ: dict[str, str], expected: tuple[str, int, bool]
) -> None:
    assert healthcheck._target(environ) == expected


BOOLEAN_SPELLINGS = [
    "",
    "true",
    "TRUE",
    "True",
    "1",
    "t",
    "yes",
    "y",
    "on",
    "On",
    "false",
    "FALSE",
    "False",
    "0",
    "f",
    "no",
    "n",
    "off",
    "Off",
]


@pytest.mark.parametrize("spelling", BOOLEAN_SPELLINGS)
def test_the_probe_reads_every_boolean_spelling_as_the_settings_do(spelling: str) -> None:
    environ = {TLS_SELF_SIGNED_VAR: spelling}

    _, _, secure = healthcheck._target(environ)

    assert secure is load_settings(environ).tls_enabled


@pytest.mark.parametrize(
    "names",
    [
        pytest.param({"dockmin_tls_self_signed": "false", "dockmin_port": "9000"}, id="lower"),
        pytest.param({"Dockmin_Tls_Self_Signed": "false", "DockMin_Port": "9000"}, id="mixed"),
    ],
)
def test_the_probe_reads_variable_names_in_any_case(names: dict[str, str]) -> None:
    assert healthcheck._target(names) == (LOOPBACK, 9000, False)


def test_the_probe_takes_the_later_of_two_names_that_differ_only_in_case() -> None:
    environ = {"DOCKMIN_TLS_SELF_SIGNED": "true", "dockmin_tls_self_signed": "false"}

    assert healthcheck._target(environ) == (LOOPBACK, 8000, False)


def test_a_healthy_plain_listener_exits_zero_and_is_asked_for_the_health_path(
    probe_env: Callable[..., None],
) -> None:
    seen: list[str] = []
    with stub_server(HTTPStatus.OK, seen) as port:
        probe_env(DOCKMIN_HOST=LOOPBACK, DOCKMIN_PORT=str(port), DOCKMIN_TLS_SELF_SIGNED="false")

        status = healthcheck.main()

    assert status == HEALTHY
    assert seen == [HEALTH_PATH]


def test_an_unhealthy_answer_exits_non_zero(
    probe_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    with stub_server(HTTPStatus.SERVICE_UNAVAILABLE, []) as port:
        probe_env(DOCKMIN_HOST=LOOPBACK, DOCKMIN_PORT=str(port), DOCKMIN_TLS_SELF_SIGNED="false")

        status = healthcheck.main()

    assert status == UNHEALTHY
    assert "503" in capsys.readouterr().err


def test_nothing_listening_exits_non_zero_without_a_traceback(
    probe_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    with stub_server(HTTPStatus.OK, []) as port:
        pass
    probe_env(DOCKMIN_HOST=LOOPBACK, DOCKMIN_PORT=str(port), DOCKMIN_TLS_SELF_SIGNED="false")

    status = healthcheck.main()

    assert status == UNHEALTHY
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("supplied", [True, False], ids=["supplied-files", "self-signed"])
def test_a_tls_listener_with_a_certificate_for_another_name_is_probed_by_address(
    probe_env: Callable[..., None], tmp_path: Path, supplied: bool
) -> None:
    pair = write_server_files(tmp_path)
    files = {"DOCKMIN_TLS_CERT_FILE": str(pair.cert), "DOCKMIN_TLS_KEY_FILE": str(pair.key)}
    seen: list[str] = []
    with stub_server(HTTPStatus.OK, seen, pair) as port:
        probe_env(
            DOCKMIN_HOST=LOOPBACK, DOCKMIN_HTTPS_PORT=str(port), **(files if supplied else {})
        )

        status = healthcheck.main()

    assert status == HEALTHY
    assert seen == [HEALTH_PATH]


def test_a_port_that_is_not_a_number_exits_non_zero(probe_env: Callable[..., None]) -> None:
    probe_env(DOCKMIN_PORT="eighty", DOCKMIN_TLS_SELF_SIGNED="false")

    assert healthcheck.main() == UNHEALTHY
