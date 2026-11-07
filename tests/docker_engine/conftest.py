"""Fixtures for the Docker access layer: a fake Engine, clients over it, and TLS endpoints."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

import app.docker_engine.transport as transport
from app.docker_engine import EngineClient
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.transport import TcpSettings, TlsMaterial, configure_tls_workdir
from tests.docker_engine.fake_engine import FakeEngine, serve_tcp
from tests.docker_engine.helpers import ENV_ID
from tests.docker_engine.pki import LOOPBACK, Pki, build_pki, server_ssl_context


@pytest.fixture(autouse=True)
def tls_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the TLS work directory under tmp_path, restoring the module-level setting afterwards."""
    monkeypatch.setattr(transport, "_tls_workdir_base", None)
    return configure_tls_workdir(tmp_path)


@pytest.fixture
def engine() -> FakeEngine:
    """Return a fake Engine speaking API 1.51 with a minimum of 1.44."""
    return FakeEngine()


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[[FakeEngine], EngineClient]]:
    """Return a factory for clients over a fake; every client is closed at teardown."""
    created: list[EngineClient] = []

    def build(fake: FakeEngine) -> EngineClient:
        client = EngineClient(ENV_ID, fake.bundle())
        created.append(client)
        return client

    yield build
    for client in created:
        await client.aclose()


@pytest.fixture
def engine_client(
    engine: FakeEngine, make_client: Callable[[FakeEngine], EngineClient]
) -> EngineClient:
    """Return a client over the default fake."""
    return make_client(engine)


@pytest.fixture
async def engine_http(engine: FakeEngine) -> AsyncIterator[EngineHttp]:
    """Return the HTTP layer over the default fake."""
    http = EngineHttp(ENV_ID, engine.bundle())
    yield http
    await http.aclose()


@pytest.fixture(scope="session")
def pki() -> Pki:
    """Return one throwaway PKI for the whole run; it is immutable and held only in memory."""
    return build_pki()


@pytest.fixture
def tls_material(pki: Pki) -> TlsMaterial:
    """Return client TLS material that trusts the test CA."""
    return TlsMaterial(pki.ca_pem, pki.client.cert_pem, pki.client.key_pem)


@pytest.fixture
async def tls_port(engine: FakeEngine, pki: Pki) -> AsyncIterator[int]:
    """Serve the fake over mutual TLS with a certificate from the test CA."""
    context = server_ssl_context(pki.server, pki.ca_pem)
    async with serve_tcp(engine, context) as port:
        yield port


@pytest.fixture
async def untrusted_port(engine: FakeEngine, pki: Pki) -> AsyncIterator[int]:
    """Serve the fake over TLS with a certificate from a CA the client does not trust."""
    context = server_ssl_context(pki.rogue_server, pki.ca_pem)
    async with serve_tcp(engine, context) as port:
        yield port


@pytest.fixture
def tls_settings(tls_port: int, tls_material: TlsMaterial) -> TcpSettings:
    """Return settings for the trusted TLS endpoint."""
    return TcpSettings(LOOPBACK, tls_port, tls_material)


@pytest.fixture
def untrusted_settings(untrusted_port: int, tls_material: TlsMaterial) -> TcpSettings:
    """Return settings for the endpoint with an untrusted certificate."""
    return TcpSettings(LOOPBACK, untrusted_port, tls_material)
