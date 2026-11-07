"""Fixtures shared by the notification tests: certificates, local receivers and a poke counter."""

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import pytest

from app.registry_client.tls import registry_context
from app.services.activity import install_notification_hook
from tests.notification_support import (
    HttpReceiver,
    Misbehaviour,
    SmtpSecurity,
    SmtpSink,
)
from tests.registry_client.loopback import Certificates, Identity, build_certificates

type StartReceiver = Callable[..., Awaitable[HttpReceiver]]
type StartSink = Callable[..., Awaitable[SmtpSink]]


@pytest.fixture
def poked() -> Iterator[list[int]]:
    """Count the times the notification hook is called; the hook is removed afterwards."""
    calls: list[int] = []
    install_notification_hook(lambda: calls.append(1))
    yield calls
    install_notification_hook(None)


@pytest.fixture(scope="session")
def certificates() -> Certificates:
    return build_certificates()


@pytest.fixture
def trusting(certificates: Certificates) -> ssl.SSLContext:
    """A client context that trusts the throwaway authority and nothing else."""
    return registry_context(verify_tls=True, ca_bundle=certificates.ca_pem)


@pytest.fixture
async def receivers(permit_loopback: None) -> AsyncIterator[StartReceiver]:
    """Return a starter for HTTP receivers on loopback; each is closed after the test."""
    started: list[HttpReceiver] = []

    async def start(
        *,
        status: int = 204,
        reply_body: bytes = b"",
        headers: dict[str, str] | None = None,
        delay: float = 0.0,
        identity: Identity | None = None,
    ) -> HttpReceiver:
        receiver = await HttpReceiver(
            status=status, reply_body=reply_body, headers=headers, delay=delay, identity=identity
        ).start()
        started.append(receiver)
        return receiver

    yield start
    for receiver in started:
        await receiver.close()


@pytest.fixture
async def sinks(certificates: Certificates, permit_loopback: None) -> AsyncIterator[StartSink]:
    """Return a starter for SMTP sinks on loopback; each is closed after the test."""
    started: list[SmtpSink] = []

    async def start(
        security: SmtpSecurity = "starttls",
        *,
        identity: Identity | None = None,
        offer_starttls: bool = True,
        refused_recipients: frozenset[str] = frozenset(),
        misbehave: Misbehaviour | None = None,
    ) -> SmtpSink:
        sink = await SmtpSink(
            security,
            identity or certificates.server,
            offer_starttls=offer_starttls,
            refused_recipients=refused_recipients,
            misbehave=misbehave,
        ).start()
        started.append(sink)
        return sink

    yield start
    for sink in started:
        await sink.close()
