"""Fixtures for the registry client: throwaway certificates and loopback servers."""

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from tests.registry_client.loopback import (
    Certificates,
    Handler,
    Identity,
    LoopbackServer,
    build_certificates,
    serve,
    serve_not_tls,
)

type Serve = Callable[..., Awaitable[LoopbackServer]]


@pytest.fixture(scope="session")
def certificates() -> Certificates:
    return build_certificates()


@pytest.fixture
async def servers(certificates: Certificates, permit_loopback: None) -> AsyncIterator[Serve]:
    """Return a starter for TLS servers presenting the trusted identity unless told otherwise."""
    started: list[LoopbackServer] = []

    async def start(handler: Handler, *, identity: Identity | None = None) -> LoopbackServer:
        server = await serve(handler, identity=identity or certificates.server)
        started.append(server)
        return server

    yield start
    for server in started:
        await server.close()


@pytest.fixture
async def not_tls(
    permit_loopback: None,
) -> AsyncIterator[Callable[[bytes | None], Awaitable[LoopbackServer]]]:
    """Return a starter for listeners that answer a TLS hello in clear text."""
    started: list[LoopbackServer] = []

    async def start(reply: bytes | None) -> LoopbackServer:
        server = await serve_not_tls(reply)
        started.append(server)
        return server

    yield start
    for server in started:
        await server.close()
