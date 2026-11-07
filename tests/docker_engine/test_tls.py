"""Tests for TLS verification, key hygiene and transport parity against real loopback sockets."""

import contextlib
import inspect
import logging
import os
import re
import ssl
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

import app.docker_engine as package
import app.docker_engine.transport as transport
from app.docker_engine import EngineClient, EngineClientRegistry, EnvironmentUnreachable
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.transport import (
    TcpSettings,
    TlsMaterial,
    TransportBundle,
    UnixSocketSettings,
    configure_tls_workdir,
    create_transport,
    open_hijack,
    parse_endpoint,
)
from tests.docker_engine.fake_engine import (
    FakeEngine,
    Reply,
    json_reply,
    serve_tcp,
    serve_unix,
)
from tests.docker_engine.helpers import (
    CONNECT_TIMEOUT_SECONDS,
    CONTAINER_ID,
    CONTAINER_SUMMARY,
    ENV_ID,
    failure_texts,
)
from tests.docker_engine.pki import LOOPBACK, Pki, key_fragments, server_ssl_context

MULTIPLEXED = "application/vnd.docker.multiplexed-stream"
HOST = "engine.test"
PORT = 2376
OWNER_ONLY = 0o600
IDENTITY_FILE_NAME = "identity.pem"
TLS_WORKDIR_NAME = "tls-work"
SKIP_ON_WINDOWS = pytest.mark.skipif(
    sys.platform == "win32", reason="asyncio cannot serve Unix sockets on Windows"
)
VERIFICATION_SWITCH = re.compile(
    r"verify|insecure|skip|disable|unsafe|check_hostname|cert_reqs|trust", re.IGNORECASE
)
SOURCE_THAT_WEAKENS_VERIFICATION = (
    "os.environ",
    "getenv",
    "CERT_NONE",
    "CERT_OPTIONAL",
    "check_hostname = False",
    "verify=False",
    "_create_unverified_context",
)
DISABLING_ENVIRONMENT = {
    "PYTHONHTTPSVERIFY": "0",
    "DOCKER_TLS_VERIFY": "0",
    "SSL_CERT_FILE": "",
    "SSL_CERT_DIR": "",
    "REQUESTS_CA_BUNDLE": "",
    "CURL_CA_BUNDLE": "",
    "DOCKMIN_TLS_VERIFY": "false",
    "DOCKMIN_INSECURE": "true",
}


def _variants(pki: Pki) -> dict[str, TlsMaterial]:
    return {
        "valid": TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.client.key_pem),
        "bad_ca": TlsMaterial("not a certificate", pki.client.cert_pem, pki.client.key_pem),
        "empty_ca": TlsMaterial("", pki.client.cert_pem, pki.client.key_pem),
        "garbage_key": TlsMaterial(pki.ca_pem, pki.client.cert_pem, "not a key"),
        "mismatched_key": TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.server.key_pem),
    }


def _build(material: TlsMaterial) -> ValueError | None:
    try:
        material.ssl_context()
    except ValueError as exc:
        return exc
    return None


def _texts_after_building(material: TlsMaterial) -> list[str]:
    failure = _build(material)
    return [
        repr(material),
        repr(TcpSettings(HOST, PORT, material)),
        *([] if failure is None else failure_texts(failure)),
    ]


@pytest.fixture
def hostile_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every temp-directory lookup at an empty directory a context build must not touch."""
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(hostile))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(hostile))
    return hostile


@pytest.fixture
def identity_loads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """Record the path and content of every identity file handed to the SSL context."""
    seen: list[tuple[Path, str]] = []
    original = ssl.SSLContext.load_cert_chain

    def spy(
        self: ssl.SSLContext,
        certfile: Path,
        keyfile: str | None = None,
        password: str | None = None,
    ) -> None:
        seen.append((Path(certfile), Path(certfile).read_text(encoding="utf-8")))
        original(self, certfile, keyfile, password)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    return seen


@pytest.fixture
def symlinked_workdir(tmp_path: Path) -> Path:
    """Return a data directory whose TLS work directory is a symlink to somewhere else."""
    data_dir = tmp_path / "linked-data"
    data_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (data_dir / TLS_WORKDIR_NAME).symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra privileges on this platform")
    return data_dir


@pytest.mark.usefixtures("permit_loopback")
async def test_a_tls_endpoint_with_a_certificate_from_the_supplied_ca_serves_calls(
    engine: FakeEngine, tls_settings: TcpSettings, pki: Pki, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    engine.route("GET", "/containers/json", json_reply([CONTAINER_SUMMARY]))

    async with EngineClient(ENV_ID, tls_settings.create_transport()) as client:
        containers = await client.containers.list()

    assert [c.id for c in containers] == ["c0ffee"]
    assert engine.paths() == ["GET /_ping", "GET /version", "GET /containers/json"]
    assert not any(f in caplog.text for f in key_fragments(pki.client.key_pem))


async def test_a_tls_endpoint_with_an_untrusted_certificate_is_unreachable_and_gets_no_request(
    engine: FakeEngine, untrusted_settings: TcpSettings
) -> None:
    async with EngineClient(ENV_ID, untrusted_settings.create_transport()) as client:
        with pytest.raises(EnvironmentUnreachable) as raised:
            await client.containers.list()

    assert raised.value.environment_id == ENV_ID
    assert engine.requests == []


async def test_the_environment_cannot_make_an_untrusted_certificate_acceptable(
    engine: FakeEngine, untrusted_settings: TcpSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", {**os.environ, **DISABLING_ENVIRONMENT})
    async with EngineClient(ENV_ID, untrusted_settings.create_transport()) as client:
        with pytest.raises(EnvironmentUnreachable):
            await client.containers.list()

    assert engine.requests == []


@pytest.mark.usefixtures("permit_loopback")
async def test_the_raw_stream_used_for_exec_also_rejects_an_untrusted_certificate(
    untrusted_settings: TcpSettings,
) -> None:
    with pytest.raises(ssl.SSLCertVerificationError):
        await untrusted_settings.open_stream(CONNECT_TIMEOUT_SECONDS)


@pytest.mark.usefixtures("permit_loopback")
async def test_hijack_over_a_verified_tls_connection_returns_the_raw_stream(
    engine: FakeEngine, tls_settings: TcpSettings
) -> None:
    attach = f"/containers/{CONTAINER_ID}/attach"
    engine.route("POST", attach, Reply(101, b"hello", {"Content-Type": MULTIPLEXED}))

    connection = await open_hijack(tls_settings, f"/v1.51{attach}")
    payload = await connection.readexactly(5)
    await connection.close()

    assert payload == b"hello"
    assert connection.multiplexed


@contextlib.asynccontextmanager
async def _unix_bundle(
    engine: FakeEngine, pki: Pki, tls_material: TlsMaterial
) -> AsyncIterator[TransportBundle]:
    # A short prefix keeps the socket path inside the platform's sun_path limit.
    with tempfile.TemporaryDirectory(prefix="dmk") as directory:
        path = str(Path(directory) / "e.sock")
        async with serve_unix(engine, path):
            yield create_transport(UnixSocketSettings(path))


@contextlib.asynccontextmanager
async def _tls_bundle(
    engine: FakeEngine, pki: Pki, tls_material: TlsMaterial
) -> AsyncIterator[TransportBundle]:
    async with serve_tcp(engine, server_ssl_context(pki.server, pki.ca_pem)) as port:
        yield create_transport(parse_endpoint(f"tcp://{LOOPBACK}:{port}", tls_material))


@pytest.fixture(params=[pytest.param("unix", marks=SKIP_ON_WINDOWS), "tls"])
async def served_client(
    request: pytest.FixtureRequest, engine: FakeEngine, pki: Pki, tls_material: TlsMaterial
) -> AsyncIterator[EngineClient]:
    """Return a client over a real socket: a Unix socket, then a mutual-TLS endpoint."""
    opener = {"unix": _unix_bundle, "tls": _tls_bundle}[request.param]
    async with opener(engine, pki, tls_material) as bundle:
        client = EngineClient(ENV_ID, bundle)
        yield client
        await client.aclose()


@pytest.mark.usefixtures("permit_loopback")
async def test_one_call_surface_works_over_a_unix_socket_and_over_a_tls_endpoint(
    engine: FakeEngine, served_client: EngineClient
) -> None:
    engine.route("GET", "/containers/json", json_reply([CONTAINER_SUMMARY]))

    containers = await served_client.containers.list(all=True)

    assert [c.names for c in containers] == [["/web"]]
    assert served_client.daemon is not None
    assert served_client.daemon.negotiated_api_version == "1.51"
    assert engine.paths() == ["GET /_ping", "GET /version", "GET /containers/json"]


def test_no_public_signature_or_attribute_offers_a_way_to_switch_verification_off() -> None:
    surfaces: list[Callable[..., Any]] = [
        TlsMaterial,
        TcpSettings,
        UnixSocketSettings,
        parse_endpoint,
        create_transport,
        open_hijack,
        EngineClient,
        EngineHttp,
        EngineClientRegistry,
        TcpSettings.open_stream,
        TcpSettings.create_transport,
    ]
    names = [
        *(p for s in surfaces for p in inspect.signature(s).parameters),
        *(a for s in (TlsMaterial, TcpSettings) for a in dir(s) if not a.startswith("__")),
    ]

    assert [n for n in names if VERIFICATION_SWITCH.search(n)] == []


def test_no_source_file_in_the_package_reads_the_environment_or_weakens_verification() -> None:
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in Path(package.__file__ or "").parent.glob("*.py")
    }

    offending = [
        (name, needle)
        for name, text in sources.items()
        for needle in SOURCE_THAT_WEAKENS_VERIFICATION
        if needle in text
    ]

    assert offending == []


@pytest.mark.parametrize("variant", ["valid", "mismatched_key"])
def test_building_a_context_leaves_no_file_behind_on_success_or_failure(
    pki: Pki, tls_workdir: Path, variant: str
) -> None:
    material = _variants(pki)[variant]

    _build(material)

    assert list(tls_workdir.iterdir()) == []


def test_the_client_key_reaches_disk_only_in_encrypted_form(
    pki: Pki, tls_material: TlsMaterial, identity_loads: list[tuple[Path, str]]
) -> None:
    tls_material.ssl_context()

    _, content = identity_loads[0]
    assert "ENCRYPTED PRIVATE KEY" in content
    assert not any(fragment in content for fragment in key_fragments(pki.client.key_pem))


def test_the_identity_file_goes_to_the_pinned_work_directory_and_never_to_a_hostile_tmpdir(
    tls_material: TlsMaterial,
    tls_workdir: Path,
    hostile_tmpdir: Path,
    identity_loads: list[tuple[Path, str]],
) -> None:
    tls_material.ssl_context()

    path, _ = identity_loads[0]
    assert path.parent.parent == tls_workdir
    assert list(hostile_tmpdir.iterdir()) == []


def test_the_identity_file_is_created_exclusively_with_owner_only_permissions(
    tls_material: TlsMaterial, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[int, int]] = []
    real_open = os.open

    def spy(path: str | Path, flags: int, mode: int = 0o777, **kwargs: int) -> int:
        if Path(path).name == IDENTITY_FILE_NAME:
            opened.append((flags, mode))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)

    tls_material.ssl_context()

    [(flags, mode)] = opened
    assert flags & os.O_EXCL
    assert mode == OWNER_ONLY


def test_building_a_context_before_the_work_directory_is_configured_raises_and_uses_no_tmpdir(
    tls_material: TlsMaterial, hostile_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_tls_workdir_base", None)

    with pytest.raises(RuntimeError, match="not configured"):
        tls_material.ssl_context()

    assert list(hostile_tmpdir.iterdir()) == []


def test_configuring_the_work_directory_creates_it_beneath_the_data_directory(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    base = configure_tls_workdir(data_dir)

    assert base == (data_dir / TLS_WORKDIR_NAME).resolve()
    assert base.is_dir()


def test_configuring_the_work_directory_refuses_a_symlink(symlinked_workdir: Path) -> None:
    with pytest.raises(ValueError, match="symlink"):
        configure_tls_workdir(symlinked_workdir)


@pytest.mark.usefixtures("permit_loopback")
async def test_the_pem_fields_are_blank_once_the_context_is_built_and_the_context_still_works(
    engine: FakeEngine, tls_settings: TcpSettings, tls_material: TlsMaterial
) -> None:
    engine.route("GET", "/containers/json", json_reply([CONTAINER_SUMMARY]))

    async with EngineClient(ENV_ID, tls_settings.create_transport()) as client:
        containers = await client.containers.list()

    pem_fields = (
        tls_material.ca_cert_pem,
        tls_material.client_cert_pem,
        tls_material.client_key_pem,
    )
    assert [c.id for c in containers] == ["c0ffee"]
    assert pem_fields == ("", "", "")


@pytest.mark.parametrize("variant", ["valid", "bad_ca", "garbage_key", "mismatched_key"])
def test_the_client_key_reaches_no_log_error_traceback_or_repr(
    pki: Pki, caplog: pytest.LogCaptureFixture, variant: str
) -> None:
    caplog.set_level(logging.DEBUG)
    material = _variants(pki)[variant]

    emitted = [caplog.text, *_texts_after_building(material), caplog.text]

    fragments = [*key_fragments(pki.client.key_pem), *key_fragments(pki.server.key_pem)]
    assert not any(fragment in text for text in emitted for fragment in fragments)
