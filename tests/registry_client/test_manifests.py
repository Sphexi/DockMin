"""The manifest digest client against loopback TLS registries and token services."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.netcheck import AddrInfo
from app.registry_client import wire
from app.registry_client.auth_config import basic_token
from app.registry_client.manifests import (
    MANIFEST_ACCEPT,
    ManifestCheckFailed,
    ManifestCheckKind,
    ManifestClient,
    ManifestTimeouts,
)
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.providers import parse_extras
from app.registry_client.references import ImageReference, parse_reference
from tests.registry_client.conftest import Serve
from tests.registry_client.loopback import (
    LOOPBACK,
    REGISTRY_NAME,
    Certificates,
    Hangup,
    LoopbackServer,
    Request,
    Stall,
    closed_port,
    http_reply,
    serve_not_tls,
)

USERNAME = "service-account"
SECRET = "manifest-secret-marker-7731"
TOKEN = "issued-bearer-token-5521"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
REPOSITORY_PATH = "org/app"
TAG = "1"
MANIFEST_TARGET = f"/v2/{REPOSITORY_PATH}/manifests/{TAG}"
SERVICE = "registry.test"
FAST = ManifestTimeouts(dns=0.2, tcp=0.5, exchange=0.3, total=3.0)
OVERSIZED_BODY = b"x" * (wire.MAX_BODY_BYTES + 1)

type Reply = Callable[[Request], bytes]
type Digest = Callable[..., Awaitable[str]]


def with_digest(digest: str = DIGEST) -> bytes:
    return http_reply(200, b"", {"Docker-Content-Digest": digest})


def registry_ok(request: Request) -> bytes:
    return with_digest()


def challenging(challenge: str) -> bytes:
    return http_reply(401, b"", {"WWW-Authenticate": challenge})


def bearer_registry(realm: str, token: str = TOKEN) -> Reply:
    challenge = f'Bearer realm="{realm}",service="{SERVICE}"'

    def reply(request: Request) -> bytes:
        if request.header("authorization") == f"Bearer {token}":
            return with_digest()
        return challenging(challenge)

    return reply


def token_service(body: bytes | None = None, status: int = 200) -> Reply:
    document = body if body is not None else json.dumps({"token": TOKEN}).encode()

    def reply(request: Request) -> bytes:
        return http_reply(status, document)

    return reply


def basic_header() -> str:
    return "Basic " + basic_token(USERNAME, SecretStr(SECRET)).get_secret_value()


def login() -> RegistryLogin:
    return RegistryLogin(USERNAME, SecretStr(SECRET))


def reference_at(address: str) -> ImageReference:
    return parse_reference(f"{address}/{REPOSITORY_PATH}:{TAG}")


def target_for(
    address: str, certificates: Certificates, *, verify_tls: bool = True
) -> RegistryTarget:
    return RegistryTarget(
        provider="custom",
        address=address,
        verify_tls=verify_tls,
        ca_bundle=certificates.ca_pem if verify_tls else None,
        extras=parse_extras("custom", {}),
    )


async def unknown_name(host: str, port: int) -> Sequence[AddrInfo]:
    raise socket.gaierror(socket.EAI_NONAME, "unknown")


async def never_resolves(host: str, port: int) -> Sequence[AddrInfo]:
    raise AssertionError("the client tried to resolve a name")


@pytest.fixture
async def manifests() -> AsyncIterator[ManifestClient]:
    client = ManifestClient(timeouts=FAST)
    yield client
    await client.aclose()


@pytest.fixture
async def head(manifests: ManifestClient, certificates: Certificates) -> Digest:
    async def run(
        server: LoopbackServer,
        who: RegistryLogin | None = None,
        *,
        verify_tls: bool = True,
    ) -> str:
        address = f"{LOOPBACK}:{server.port}"
        return await manifests.head_digest(
            target_for(address, certificates, verify_tls=verify_tls), reference_at(address), who
        )

    return run


async def failure_of(call: Awaitable[str]) -> ManifestCheckFailed:
    with pytest.raises(ManifestCheckFailed) as raised:
        await call
    return raised.value


async def test_an_anonymous_head_reads_the_digest_with_the_manifest_media_types(
    head: Digest, servers: Serve
) -> None:
    registry = await servers(registry_ok)

    digest = await head(registry)

    [sent] = registry.requests
    assert digest == DIGEST
    assert (sent.method, sent.target) == ("HEAD", MANIFEST_TARGET)
    assert sent.header("accept") == MANIFEST_ACCEPT
    assert sent.header("authorization") is None


async def test_a_bearer_challenge_is_answered_anonymously_with_a_scoped_token_request(
    head: Digest, servers: Serve
) -> None:
    tokens = await servers(token_service())
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    digest = await head(registry)

    [token_request] = tokens.requests
    assert digest == DIGEST
    assert token_request.method == "GET"
    assert token_request.target == (
        f"/token?service={SERVICE}&scope=repository%3A{REPOSITORY_PATH.replace('/', '%2F')}%3Apull"
    )
    assert token_request.header("authorization") is None
    assert [r.method for r in registry.requests] == ["HEAD", "HEAD"]
    assert registry.requests[1].header("authorization") == f"Bearer {TOKEN}"


async def test_a_bearer_challenge_with_a_login_sends_basic_to_the_token_service_only(
    head: Digest, servers: Serve
) -> None:
    tokens = await servers(token_service())
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    digest = await head(registry, login())

    assert digest == DIGEST
    assert tokens.requests[0].header("authorization") == basic_header()
    assert [r.header("authorization") for r in registry.requests] == [None, f"Bearer {TOKEN}"]


async def test_a_basic_challenge_is_answered_with_the_login(head: Digest, servers: Serve) -> None:
    def reply(request: Request) -> bytes:
        if request.header("authorization") == basic_header():
            return with_digest()
        return challenging('Basic realm="registry"')

    registry = await servers(reply)

    digest = await head(registry, login())

    assert digest == DIGEST
    assert [r.method for r in registry.requests] == ["HEAD", "HEAD"]


async def test_a_basic_challenge_without_a_login_fails_authentication_after_one_request(
    head: Digest, servers: Serve
) -> None:
    registry = await servers(lambda r: challenging('Basic realm="registry"'))

    failure = await failure_of(head(registry))

    assert failure.kind is ManifestCheckKind.AUTHENTICATION
    assert len(registry.requests) == 1


async def test_a_plain_http_token_realm_is_refused_and_nothing_is_sent_to_it(
    head: Digest, servers: Serve
) -> None:
    tokens = await servers(token_service())
    registry = await servers(bearer_registry(f"http://{LOOPBACK}:{tokens.port}/token"))

    failure = await failure_of(head(registry, login()))

    assert failure.kind is ManifestCheckKind.AUTHENTICATION
    assert tokens.requests == []
    assert len(registry.requests) == 1


@pytest.mark.parametrize(
    ("token_reply", "kind"),
    [
        pytest.param(http_reply(401, b"{}"), ManifestCheckKind.AUTHENTICATION, id="denied"),
        pytest.param(http_reply(403, b"{}"), ManifestCheckKind.AUTHENTICATION, id="forbidden"),
        pytest.param(http_reply(429, b"{}"), ManifestCheckKind.RATE_LIMITED, id="rate-limited"),
        pytest.param(http_reply(404, b"{}"), ManifestCheckKind.PROTOCOL, id="no-such-service"),
        pytest.param(http_reply(200, b"not json"), ManifestCheckKind.PROTOCOL, id="not-json"),
        pytest.param(http_reply(200, b'{"token": ""}'), ManifestCheckKind.PROTOCOL, id="no-token"),
        pytest.param(
            http_reply(200, b'{"token": "bad token"}'),
            ManifestCheckKind.PROTOCOL,
            id="unsafe-token",
        ),
        pytest.param(
            http_reply(200, OVERSIZED_BODY), ManifestCheckKind.PROTOCOL, id="oversized-reply"
        ),
        pytest.param(
            http_reply(302, b"", {"Location": "https://elsewhere.test/token"}),
            ManifestCheckKind.PROTOCOL,
            id="redirect-not-followed",
        ),
    ],
)
async def test_a_token_service_that_does_not_issue_a_token_fails_with_its_kind(
    head: Digest, servers: Serve, token_reply: bytes, kind: ManifestCheckKind
) -> None:
    tokens = await servers(lambda r: token_reply)
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    failure = await failure_of(head(registry))

    assert failure.kind is kind
    assert len(registry.requests) == 1


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        pytest.param(http_reply(404), ManifestCheckKind.NOT_FOUND, id="not-found"),
        pytest.param(http_reply(403), ManifestCheckKind.AUTHENTICATION, id="forbidden"),
        pytest.param(http_reply(429), ManifestCheckKind.RATE_LIMITED, id="rate-limited"),
        pytest.param(http_reply(500), ManifestCheckKind.PROTOCOL, id="server-error"),
        pytest.param(
            http_reply(301, b"", {"Location": "https://elsewhere.test/v2/"}),
            ManifestCheckKind.PROTOCOL,
            id="redirect-not-followed",
        ),
        pytest.param(http_reply(200), ManifestCheckKind.NO_DIGEST, id="no-digest-header"),
        pytest.param(
            with_digest("sha256:short"), ManifestCheckKind.PROTOCOL, id="malformed-digest"
        ),
        pytest.param(with_digest("sha512:" + "a" * 64), ManifestCheckKind.PROTOCOL, id="algorithm"),
        pytest.param(
            http_reply(
                200,
                b"",
                {"Docker-Content-Digest": DIGEST, "docker-content-digest": OTHER_DIGEST},
            ),
            ManifestCheckKind.PROTOCOL,
            id="two-digests",
        ),
        pytest.param(
            challenging("Negotiate"), ManifestCheckKind.AUTHENTICATION, id="unsupported-challenge"
        ),
        pytest.param(b"this is not http\r\n\r\n", ManifestCheckKind.PROTOCOL, id="not-http"),
    ],
)
async def test_a_registry_reply_that_carries_no_usable_digest_fails_with_its_kind(
    head: Digest, servers: Serve, reply: bytes, kind: ManifestCheckKind
) -> None:
    registry = await servers(lambda r: reply)

    failure = await failure_of(head(registry))

    assert failure.kind is kind


async def test_a_repeated_identical_digest_header_is_accepted(head: Digest, servers: Serve) -> None:
    both = http_reply(200, b"", {"Docker-Content-Digest": DIGEST})
    registry = await servers(lambda r: both)

    assert await head(registry) == DIGEST


@pytest.mark.usefixtures("permit_loopback")
async def test_a_closed_port_fails_at_tcp(
    manifests: ManifestClient, certificates: Certificates
) -> None:
    address = f"{LOOPBACK}:{closed_port()}"

    failure = await failure_of(
        manifests.head_digest(target_for(address, certificates), reference_at(address), None)
    )

    assert failure.kind is ManifestCheckKind.TCP


async def test_a_name_that_does_not_resolve_fails_at_dns(certificates: Certificates) -> None:
    client = ManifestClient(resolver=unknown_name, timeouts=FAST)
    address = f"{REGISTRY_NAME}:5000"

    failure = await failure_of(
        client.head_digest(target_for(address, certificates), reference_at(address), None)
    )

    assert failure.kind is ManifestCheckKind.DNS


@pytest.mark.usefixtures("permit_loopback")
async def test_a_listener_that_does_not_speak_tls_fails_the_handshake(
    manifests: ManifestClient, certificates: Certificates
) -> None:
    listener = await serve_not_tls(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
    address = f"{LOOPBACK}:{listener.port}"

    try:
        failure = await failure_of(
            manifests.head_digest(target_for(address, certificates), reference_at(address), None)
        )
    finally:
        await listener.close()

    assert failure.kind is ManifestCheckKind.TLS


async def test_a_certificate_from_an_untrusted_authority_fails_certificate(
    head: Digest, servers: Serve, certificates: Certificates
) -> None:
    registry = await servers(registry_ok, identity=certificates.rogue_server)

    failure = await failure_of(head(registry))

    assert failure.kind is ManifestCheckKind.CERTIFICATE
    assert registry.requests == []


async def test_a_stalled_registry_times_out(head: Digest, servers: Serve) -> None:
    registry = await servers(lambda r: Stall())

    failure = await failure_of(head(registry))

    assert failure.kind is ManifestCheckKind.TIMEOUT


async def test_a_registry_that_hangs_up_without_answering_is_a_protocol_failure(
    head: Digest, servers: Serve
) -> None:
    registry = await servers(lambda r: Hangup())

    failure = await failure_of(head(registry))

    assert failure.kind is ManifestCheckKind.PROTOCOL


async def test_a_failure_carries_a_product_message_and_no_secret_or_exception_context(
    head: Digest, servers: Serve
) -> None:
    tokens = await servers(token_service(status=401))
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    failure = await failure_of(head(registry, login()))

    texts = [str(failure), repr(failure), failure.message, *map(repr, failure.args)]
    assert not any(SECRET in text or USERNAME in text for text in texts)
    assert failure.__cause__ is None
    assert failure.__context__ is None


@pytest.mark.parametrize(
    "address",
    ["registry.example.com", "mirror.example.com:5000"],
    ids=["another-host", "another-port"],
)
async def test_a_target_at_another_registry_than_the_reference_is_refused_before_connecting(
    address: str, certificates: Certificates
) -> None:
    client = ManifestClient(resolver=never_resolves, timeouts=FAST)
    reference = parse_reference(f"registry.example.com:6000/{REPOSITORY_PATH}:{TAG}")

    with pytest.raises(ValueError, match="not the registry"):
        await client.head_digest(target_for(address, certificates), reference, None)


async def test_a_pinned_reference_is_refused_before_connecting(certificates: Certificates) -> None:
    client = ManifestClient(resolver=never_resolves, timeouts=FAST)
    reference = parse_reference(f"registry.example.com/{REPOSITORY_PATH}@{DIGEST}")

    with pytest.raises(ValueError, match="no tag"):
        await client.head_digest(target_for("registry.example.com", certificates), reference, None)


async def test_credentials_without_certificate_verification_are_refused_before_connecting(
    certificates: Certificates,
) -> None:
    client = ManifestClient(resolver=never_resolves, timeouts=FAST)
    target = target_for("registry.example.com", certificates, verify_tls=False)

    with pytest.raises(ValueError, match="certificate verification"):
        await client.head_digest(target, reference_at("registry.example.com"), login())


async def test_a_closed_client_refuses_further_checks(certificates: Certificates) -> None:
    client = ManifestClient(resolver=never_resolves, timeouts=FAST)
    await client.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await client.head_digest(
            target_for("registry.example.com", certificates),
            reference_at("registry.example.com"),
            None,
        )


class RecordingWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)


@pytest.mark.parametrize(
    ("target", "host_header", "headers"),
    [
        pytest.param("/v2/x\r\nX: y", "registry.test", {}, id="line-break-in-the-target"),
        pytest.param("/v2/x", "registry.test\r\nX: y", {}, id="line-break-in-the-host"),
        pytest.param("/v2/x", "registry.test", {"Host": "other"}, id="host-header"),
        pytest.param("/v2/x", "registry.test", {"Connection": "keep-alive"}, id="connection"),
        pytest.param("/v2/x", "registry.test", {"Accept-Encoding": "gzip"}, id="encoding"),
        pytest.param("/v2/x", "registry.test", {"Content-Length": "5"}, id="content-length"),
        pytest.param("/v2/x", "registry.test", {"Transfer-Encoding": "chunked"}, id="transfer"),
        pytest.param("/v2/x", "registry.test", {"X-A": "b\r\nX-C: d"}, id="line-break-in-a-value"),
    ],
)
async def test_head_refuses_anything_that_could_split_or_override_the_request_before_writing(
    target: str, host_header: str, headers: dict[str, str]
) -> None:
    writer = RecordingWriter()

    with pytest.raises(ValueError):
        await wire.head(
            cast(Any, asyncio.StreamReader()),
            cast(Any, writer),
            target=target,
            host_header=host_header,
            headers=headers,
        )

    assert writer.written == []


async def test_a_registry_on_an_internal_address_is_refused_before_any_connection(
    head: Digest, servers: Serve, address_policy: Callable[..., None]
) -> None:
    server = await servers(registry_ok)
    address_policy()

    failure = await failure_of(head(server))

    assert failure.kind is ManifestCheckKind.ADDRESS_REFUSED
    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in str(failure)
    assert server.requests == []


async def test_a_registry_on_a_permitted_internal_address_is_reached(
    head: Digest, servers: Serve, address_policy: Callable[..., None]
) -> None:
    server = await servers(registry_ok)
    address_policy("127.0.0.0/8")

    digest = await head(server)

    assert digest and len(server.requests) == 1


async def test_a_public_only_target_on_a_private_address_is_refused_before_any_connection(
    servers: Serve, certificates: Certificates, address_policy: Callable[..., None]
) -> None:
    server = await servers(registry_ok)
    address_policy("127.0.0.0/8")
    client = ManifestClient(timeouts=FAST)
    address = f"{LOOPBACK}:{server.port}"
    target = replace(target_for(address, certificates), public_only=True)

    failure = await failure_of(client.head_digest(target, reference_at(address), None))
    await client.aclose()

    assert failure.kind is ManifestCheckKind.PRIVATE_ADDRESS
    assert server.requests == []


async def test_a_token_service_on_an_internal_address_is_refused_and_the_credentials_stay_home(
    head: Digest, servers: Serve, address_policy: Callable[..., None]
) -> None:
    registry = await servers(bearer_registry("https://169.254.169.254/token"))
    address_policy("127.0.0.0/8")

    failure = await failure_of(head(registry, RegistryLogin("user", SecretStr(SECRET))))

    assert failure.kind is ManifestCheckKind.ADDRESS_REFUSED
    assert len(registry.requests) == 1


async def test_a_token_service_name_that_resolves_to_an_internal_address_is_refused(
    servers: Serve,
    certificates: Certificates,
    address_policy: Callable[..., None],
) -> None:
    tokens = await servers(registry_ok)
    registry = await servers(bearer_registry(f"https://tokens.example.test:{tokens.port}/token"))
    address_policy("127.0.0.0/8")
    client = ManifestClient(resolver=lambda host, port: _metadata(host, port), timeouts=FAST)
    address = f"{LOOPBACK}:{registry.port}"

    failure = await failure_of(
        client.head_digest(
            target_for(address, certificates),
            reference_at(address),
            RegistryLogin("user", SecretStr(SECRET)),
        )
    )
    await client.aclose()

    assert failure.kind is ManifestCheckKind.ADDRESS_REFUSED
    assert tokens.requests == []


async def _metadata(host: str, port: int) -> Sequence[AddrInfo]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]
