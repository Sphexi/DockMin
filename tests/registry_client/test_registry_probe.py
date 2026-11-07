"""The staged registry connection test against loopback servers: which stage fails, and how."""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.netcheck import AddrInfo, StageOutcome
from app.registry_client.auth_config import basic_token
from app.registry_client.ecr import DerivationRequest, DerivedSecret, DerivedSecretSource
from app.registry_client.errors import TokenExchangeFailed, TokenExchangeKind
from app.registry_client.probe import (
    RegistryLogin,
    RegistryProbeCoordinator,
    RegistryProbeResult,
    RegistryProbeTimeouts,
    RegistryStage,
    RegistryTarget,
)
from app.registry_client.providers import ProviderKind, parse_extras
from tests.registry_client.conftest import Serve
from tests.registry_client.loopback import (
    ECR_NAME,
    LOOPBACK,
    REGISTRY_NAME,
    TOKEN_SERVICE_NAME,
    Certificates,
    Hangup,
    LoopbackServer,
    Request,
    Stall,
    closed_port,
    http_reply,
)
from tests.support import present

USERNAME = "service-account"
SECRET = "registry-secret-marker-7731"
ISSUED_TOKEN = "issued-bearer-token-5521"
REGISTRY_HEADER = {"Docker-Distribution-API-Version": "registry/2.0"}
STATIC_ORDER = [
    RegistryStage.DNS,
    RegistryStage.TCP,
    RegistryStage.TLS_HANDSHAKE,
    RegistryStage.CERTIFICATE,
    RegistryStage.REGISTRY_API,
    RegistryStage.AUTHENTICATION,
]
DERIVED_ORDER = [RegistryStage.CREDENTIAL_EXCHANGE, *STATIC_ORDER]
FAST = RegistryProbeTimeouts(
    credential_exchange=0.2,
    dns=0.2,
    tcp=0.5,
    tls_handshake=0.5,
    registry_api=0.2,
    authentication=0.2,
    total=5.0,
)
EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)

type Probe = Callable[..., Awaitable[RegistryProbeResult]]
type Reply = Callable[[Request], bytes]


def basic_header(username: str = USERNAME, secret: str = SECRET) -> str:
    return "Basic " + basic_token(username, SecretStr(secret)).get_secret_value()


def registry_ok(request: Request) -> bytes:
    return http_reply(200, b"{}", REGISTRY_HEADER)


def challenging(challenge: str) -> Reply:
    def reply(request: Request) -> bytes:
        return http_reply(401, b'{"errors":[]}', {**REGISTRY_HEADER, "WWW-Authenticate": challenge})

    return reply


def basic_registry(expected: str) -> Reply:
    def reply(request: Request) -> bytes:
        if request.header("authorization") == expected:
            return http_reply(200, b"{}", REGISTRY_HEADER)
        return challenging('Basic realm="registry"')(request)

    return reply


def bearer_registry(realm: str, expected_token: str = ISSUED_TOKEN) -> Reply:
    def reply(request: Request) -> bytes:
        if request.header("authorization") == f"Bearer {expected_token}":
            return http_reply(200, b"{}", REGISTRY_HEADER)
        return challenging(f'Bearer realm="{realm}",service="registry.test"')(request)

    return reply


def token_service(token_reply: bytes | None = None, *, expected: str | None = None) -> Reply:
    good = token_reply or http_reply(200, json.dumps({"token": ISSUED_TOKEN}).encode())

    def reply(request: Request) -> bytes:
        if expected is not None and request.header("authorization") != expected:
            return http_reply(401, b'{"details":"denied"}')
        return good

    return reply


def stages(result: RegistryProbeResult) -> list[RegistryStage]:
    return [stage.stage for stage in result.stages]


def outcomes(result: RegistryProbeResult) -> dict[RegistryStage, StageOutcome]:
    return {stage.stage: stage.outcome for stage in result.stages}


def message_of(result: RegistryProbeResult, stage: RegistryStage) -> str:
    return next(s.message for s in result.stages if s.stage is stage)


def target_for(
    address: str,
    *,
    provider: ProviderKind = "custom",
    verify_tls: bool = True,
    ca_bundle: str | None = None,
) -> RegistryTarget:
    extras = {"region": "eu-west-2"} if provider == "ecr" else {}
    return RegistryTarget(
        provider=provider,
        address=address,
        verify_tls=verify_tls,
        ca_bundle=ca_bundle,
        extras=parse_extras(provider, extras),
    )


def login() -> RegistryLogin:
    return RegistryLogin(USERNAME, SecretStr(SECRET))


def to_loopback(port: int | None = None) -> Callable[[str, int], Awaitable[Sequence[AddrInfo]]]:
    async def resolve(host: str, requested: int) -> Sequence[AddrInfo]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (LOOPBACK, port or requested))]

    return resolve


async def unknown_name(host: str, port: int) -> Sequence[AddrInfo]:
    raise socket.gaierror(socket.EAI_NONAME, "unknown")


async def no_addresses(host: str, port: int) -> Sequence[AddrInfo]:
    return []


async def never_answers(host: str, port: int) -> Sequence[AddrInfo]:
    await asyncio.Event().wait()
    return []


class ScriptedDeriver:
    """A deriver that issues a fixed token, fails, crashes or never answers."""

    def __init__(self, outcome: DerivedSecret | Exception | None) -> None:
        self.outcome = outcome
        self.requests: list[DerivationRequest] = []

    async def derive(self, request: DerivationRequest) -> DerivedSecret:
        self.requests.append(request)
        if self.outcome is None:
            await asyncio.Event().wait()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        assert self.outcome is not None
        return self.outcome


def issued(username: str = "AWS", secret: str = "ecr-issued-password") -> DerivedSecret:
    return DerivedSecret(basic_token(username, SecretStr(secret)), EXPIRES)


@pytest.fixture
async def probe() -> AsyncIterator[Probe]:
    """Return a runner of single tests; every coordinator it builds is closed afterwards."""
    built: list[RegistryProbeCoordinator] = []

    async def run(
        target: RegistryTarget,
        who: RegistryLogin | None = None,
        *,
        resolver: Callable[[str, int], Awaitable[Sequence[AddrInfo]]] | None = None,
        derivers: dict[str, DerivedSecretSource] | None = None,
        timeouts: RegistryProbeTimeouts = FAST,
    ) -> RegistryProbeResult:
        coordinator = RegistryProbeCoordinator(
            derivers=derivers or {}, resolver=resolver, timeouts=timeouts
        )
        built.append(coordinator)
        return await coordinator.probe(target, who, key=None)

    yield run
    for coordinator in built:
        await coordinator.aclose()


async def test_a_name_that_does_not_resolve_fails_at_dns_and_runs_nothing_after_it(
    probe: Probe,
) -> None:
    result = await probe(target_for(f"{REGISTRY_NAME}:5000"), resolver=unknown_name)

    assert result.failed_stage is RegistryStage.DNS
    assert "does not exist" in result.message
    assert stages(result) == STATIC_ORDER
    assert {o for s, o in outcomes(result).items() if s is not RegistryStage.DNS} == {
        StageOutcome.NOT_RUN
    }


@pytest.mark.parametrize(
    ("resolver", "fragment"),
    [
        (no_addresses, "no usable address"),
        (never_answers, "did not finish within"),
    ],
    ids=["empty-answer", "stalled-lookup"],
)
async def test_a_lookup_without_an_answer_fails_at_dns(
    probe: Probe, resolver: Callable[[str, int], Awaitable[Sequence[AddrInfo]]], fragment: str
) -> None:
    result = await probe(target_for(f"{REGISTRY_NAME}:5000"), resolver=resolver)

    assert result.failed_stage is RegistryStage.DNS
    assert fragment in result.message


async def test_an_ip_literal_skips_the_name_lookup(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok)

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem),
        resolver=unknown_name,
    )

    assert result.passed
    assert outcomes(result)[RegistryStage.DNS] is StageOutcome.SKIPPED


@pytest.mark.usefixtures("permit_loopback")
async def test_a_closed_port_fails_at_tcp_with_a_refusal(probe: Probe) -> None:
    result = await probe(target_for(f"{LOOPBACK}:{closed_port()}"))

    assert result.failed_stage is RegistryStage.TCP
    assert "refused" in result.message
    assert outcomes(result)[RegistryStage.TLS_HANDSHAKE] is StageOutcome.NOT_RUN


async def test_a_listener_that_does_not_speak_tls_fails_at_the_handshake(
    probe: Probe, not_tls: Callable[[bytes | None], Awaitable[LoopbackServer]]
) -> None:
    server = await not_tls(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")

    result = await probe(target_for(f"{LOOPBACK}:{server.port}"))

    assert result.failed_stage is RegistryStage.TLS_HANDSHAKE
    assert "does not speak TLS" in result.message
    assert outcomes(result)[RegistryStage.TCP] is StageOutcome.PASSED


async def test_a_listener_that_hangs_up_during_the_handshake_fails_at_the_handshake(
    probe: Probe, not_tls: Callable[[bytes | None], Awaitable[LoopbackServer]]
) -> None:
    server = await not_tls(None)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}"))

    assert result.failed_stage is RegistryStage.TLS_HANDSHAKE
    assert "closed the connection" in result.message


@pytest.mark.parametrize(
    ("which", "fragment"),
    [
        ("rogue_server", "not issued by a trusted authority"),
        ("wrong_name_server", "does not cover the host name"),
        ("expired_server", "has expired"),
    ],
)
async def test_a_certificate_fault_fails_at_the_certificate_stage_after_the_handshake_passes(
    probe: Probe, servers: Serve, certificates: Certificates, which: str, fragment: str
) -> None:
    server = await servers(registry_ok, identity=getattr(certificates, which))

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem),
    )

    assert result.failed_stage is RegistryStage.CERTIFICATE
    assert fragment in result.message
    assert outcomes(result)[RegistryStage.TLS_HANDSHAKE] is StageOutcome.PASSED
    assert outcomes(result)[RegistryStage.REGISTRY_API] is StageOutcome.NOT_RUN


async def test_without_the_private_ca_a_certificate_signed_by_it_is_not_trusted(
    probe: Probe, servers: Serve
) -> None:
    server = await servers(registry_ok)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}"))

    assert result.failed_stage is RegistryStage.CERTIFICATE
    assert "private CA" in result.message


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (
            http_reply(301, b"", {"Location": "https://elsewhere.example/v2/"}),
            "redirect (HTTP 301)",
        ),
        (http_reply(200, b"<html>hello</html>"), "not like a Docker registry"),
        (http_reply(404, b"nope"), "HTTP 404"),
        (http_reply(401, b"unauthorised"), "not like a Docker registry"),
        (http_reply(401, b"", {"WWW-Authenticate": "Negotiate"}), "not like a Docker registry"),
        (b"this is not http\r\n\r\n", "not valid HTTP"),
        (b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 70_000 + b"\r\n\r\n", "far larger"),
    ],
    ids=[
        "redirect",
        "web-page",
        "not-found",
        "401-without-challenge",
        "401-unsupported-challenge",
        "garbage",
        "oversized-head",
    ],
)
async def test_an_answer_that_is_not_a_registry_fails_at_the_registry_api_stage(
    probe: Probe, servers: Serve, certificates: Certificates, reply: bytes, fragment: str
) -> None:
    server = await servers(lambda request: reply)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert result.failed_stage is RegistryStage.REGISTRY_API
    assert fragment in result.message
    assert outcomes(result)[RegistryStage.CERTIFICATE] is StageOutcome.PASSED
    assert outcomes(result)[RegistryStage.AUTHENTICATION] is StageOutcome.NOT_RUN


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [(Stall(), "did not finish within"), (Hangup(), "closed the connection without answering")],
    ids=["stalled", "hung-up"],
)
async def test_a_server_that_does_not_answer_the_api_request_fails_at_the_registry_api_stage(
    probe: Probe,
    servers: Serve,
    certificates: Certificates,
    answer: Stall | Hangup,
    fragment: str,
) -> None:
    server = await servers(lambda request: answer)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert result.failed_stage is RegistryStage.REGISTRY_API
    assert fragment in result.message


async def test_a_challenge_with_a_registry_header_is_accepted_as_a_registry(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(challenging('Basic realm="r"'))

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert outcomes(result)[RegistryStage.REGISTRY_API] is StageOutcome.PASSED


async def test_a_challenge_without_the_registry_header_is_still_accepted_as_a_registry(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(lambda r: http_reply(401, b"", {"WWW-Authenticate": 'Basic realm="r"'}))

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert outcomes(result)[RegistryStage.REGISTRY_API] is StageOutcome.PASSED


async def test_an_anonymous_set_passes_every_stage_and_skips_authentication(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert result.passed
    assert result.failed_stage is None
    assert stages(result) == STATIC_ORDER
    assert outcomes(result) == {
        RegistryStage.DNS: StageOutcome.SKIPPED,
        RegistryStage.TCP: StageOutcome.PASSED,
        RegistryStage.TLS_HANDSHAKE: StageOutcome.PASSED,
        RegistryStage.CERTIFICATE: StageOutcome.PASSED,
        RegistryStage.REGISTRY_API: StageOutcome.PASSED,
        RegistryStage.AUTHENTICATION: StageOutcome.SKIPPED,
    }
    assert "decided per repository" in message_of(result, RegistryStage.AUTHENTICATION)
    assert [r.header("authorization") for r in server.requests] == [None]


async def test_an_unverified_anonymous_set_passes_and_says_the_certificate_was_not_checked(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok, identity=certificates.rogue_server)

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", verify_tls=False))

    assert result.passed
    assert outcomes(result)[RegistryStage.CERTIFICATE] is StageOutcome.SKIPPED
    assert "not checked" in message_of(result, RegistryStage.CERTIFICATE)


async def test_a_name_that_resolves_is_connected_to_and_verified_against_that_name(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok)

    result = await probe(
        target_for(f"{REGISTRY_NAME}:{server.port}", ca_bundle=certificates.ca_pem),
        resolver=to_loopback(),
    )

    assert result.passed
    assert outcomes(result)[RegistryStage.DNS] is StageOutcome.PASSED


async def test_a_registry_asking_for_basic_credentials_accepts_the_right_secret(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(basic_registry(basic_header()))

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.passed
    assert outcomes(result)[RegistryStage.AUTHENTICATION] is StageOutcome.PASSED
    assert "accepted the credentials" in result.message
    assert [r.header("authorization") for r in server.requests] == [None, basic_header()]


async def test_a_registry_asking_for_basic_credentials_rejects_the_wrong_secret(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(basic_registry(basic_header(secret="another-secret")))

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "rejected the credentials" in result.message
    assert outcomes(result)[RegistryStage.REGISTRY_API] is StageOutcome.PASSED


async def test_a_registry_answering_an_error_to_credentials_is_named_by_status(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    def reply(request: Request) -> bytes:
        if request.header("authorization") is None:
            return challenging('Basic realm="r"')(request)
        return http_reply(500, b"internal")

    server = await servers(reply)

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "HTTP 500" in result.message


async def test_a_registry_that_does_not_ask_for_credentials_skips_authentication(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok)

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.passed
    assert outcomes(result)[RegistryStage.AUTHENTICATION] is StageOutcome.SKIPPED
    assert "did not ask for credentials" in message_of(result, RegistryStage.AUTHENTICATION)
    assert [r.header("authorization") for r in server.requests] == [None]


async def test_a_bearer_flow_obtains_a_token_from_the_realm_and_presents_it_to_the_registry(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(token_service(expected=basic_header()))
    realm = f"https://{LOOPBACK}:{tokens.port}/token"
    registry = await servers(bearer_registry(realm))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.passed
    authentication = next(s for s in result.stages if s.stage is RegistryStage.AUTHENTICATION)
    assert authentication.outcome is StageOutcome.PASSED
    assert authentication.facts == {"method": "bearer", "token_service": LOOPBACK}
    assert [(r.target, r.header("authorization")) for r in tokens.requests] == [
        ("/token?service=registry.test", basic_header())
    ]
    assert [r.header("authorization") for r in registry.requests] == [
        None,
        f"Bearer {ISSUED_TOKEN}",
    ]


async def test_a_realm_on_another_host_name_is_resolved_and_reached(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(token_service())
    registry = await servers(bearer_registry(f"https://{TOKEN_SERVICE_NAME}:{tokens.port}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem),
        login(),
        resolver=to_loopback(),
    )

    assert result.passed
    authentication = next(s for s in result.stages if s.stage is RegistryStage.AUTHENTICATION)
    assert authentication.facts["token_service"] == TOKEN_SERVICE_NAME


async def test_a_realm_name_that_does_not_resolve_fails_authentication_by_name(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    registry = await servers(bearer_registry(f"https://{TOKEN_SERVICE_NAME}:9/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem),
        login(),
        resolver=unknown_name,
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "token service name could not be resolved" in result.message


async def test_a_realm_nothing_listens_on_fails_authentication_by_name(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{closed_port()}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "token service could not be reached" in result.message


async def test_a_plain_http_realm_fails_authentication_and_nothing_is_sent_to_it(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(token_service())
    registry = await servers(bearer_registry(f"http://{LOOPBACK}:{tokens.port}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "plain HTTP" in result.message
    assert tokens.requests == []


@pytest.mark.parametrize(
    ("challenge", "fragment"),
    [
        ('Bearer service="x"', "does not name a token service"),
        ('Bearer realm="https://user:pw@127.0.0.1:9/token"', "not usable"),
        ('Bearer realm="ftp://127.0.0.1/token"', "not usable"),
        ('Bearer realm="https://127.0.0.1:9/token#frag"', "not usable"),
        ('Bearer realm="https://127.0.0.1:9/token", service="' + "s" * 300 + '"', "not usable"),
        ("Negotiate", "does not support"),
        ("Bearer " + "a" * 5000, "malformed or too large"),
        ('Bearer realm="unterminated', "malformed or too large"),
    ],
    ids=[
        "no-realm",
        "userinfo-in-realm",
        "ftp-realm",
        "fragment-in-realm",
        "oversized-service",
        "unsupported-scheme",
        "oversized-challenge",
        "unterminated-quote",
    ],
)
async def test_an_unusable_challenge_fails_authentication_without_a_second_request(
    probe: Probe, servers: Serve, certificates: Certificates, challenge: str, fragment: str
) -> None:
    registry = await servers(
        lambda r: http_reply(401, b"", {**REGISTRY_HEADER, "WWW-Authenticate": challenge})
    )

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert fragment in result.message
    assert [r.header("authorization") for r in registry.requests] == [None]


@pytest.mark.parametrize(
    ("token_reply", "fragment"),
    [
        (http_reply(401, b'{"details":"nope"}'), "token service rejected the credentials"),
        (http_reply(503, b"down"), "token service answered HTTP 503"),
        (http_reply(200, b"not json"), "held no usable token"),
        (http_reply(200, b'{"token": ""}'), "held no usable token"),
        (http_reply(200, b'{"unrelated": "x"}'), "held no usable token"),
        (http_reply(200, b'["token"]'), "held no usable token"),
        (http_reply(200, json.dumps({"token": "has space\\r\\nX: y"}).encode()), "no usable token"),
        (http_reply(200, json.dumps({"token": "t" * 9000}).encode()), "held no usable token"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 70000\r\n\r\n", "far larger"),
    ],
    ids=[
        "rejected",
        "server-error",
        "not-json",
        "empty-token",
        "no-token-field",
        "wrong-document-type",
        "unsafe-characters",
        "oversized-token",
        "oversized-body",
    ],
)
async def test_a_token_service_that_does_not_issue_a_token_fails_authentication_by_name(
    probe: Probe,
    servers: Serve,
    certificates: Certificates,
    token_reply: bytes,
    fragment: str,
) -> None:
    tokens = await servers(lambda request: token_reply)
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert fragment in result.message
    assert [r.header("authorization") for r in registry.requests] == [None]


async def test_a_token_the_registry_refuses_is_reported_as_the_registrys_refusal(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(token_service())
    registry = await servers(
        bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token", expected_token="a-different-one")
    )

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "registry rejected the token" in result.message


async def test_a_token_service_that_stalls_fails_authentication_within_the_stage_limit(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(lambda request: Stall())
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "did not finish within" in result.message


async def test_a_token_service_with_an_untrusted_certificate_fails_authentication_by_name(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(token_service(), identity=certificates.rogue_server)
    registry = await servers(bearer_registry(f"https://{LOOPBACK}:{tokens.port}/token"))

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "token service's certificate was not issued by a trusted authority" in result.message
    assert tokens.requests == []


async def test_nothing_the_server_sends_back_reaches_the_result_and_the_secret_never_does(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    hostile = f"echo {SECRET} {basic_header()} {ISSUED_TOKEN}"

    def reply(request: Request) -> bytes:
        if request.header("authorization") is None:
            return http_reply(
                401,
                hostile.encode(),
                {
                    **REGISTRY_HEADER,
                    "WWW-Authenticate": 'Basic realm="r"',
                    "X-Echo": hostile,
                },
            )
        return http_reply(
            403,
            hostile.encode(),
            {"X-Echo": hostile, "X-Received": present(request.header("authorization"))},
        )

    server = await servers(reply)

    result = await probe(
        target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem), login()
    )

    text = repr(result) + " ".join(s.message + repr(s.facts) for s in result.stages)
    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert [m for m in (SECRET, basic_header(), ISSUED_TOKEN, "echo ") if m in text] == []


async def test_a_derived_set_exchanges_first_and_then_presents_the_issued_login(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    deriver = ScriptedDeriver(issued("AWS", "ecr-issued-password"))
    server = await servers(basic_registry(basic_header("AWS", "ecr-issued-password")))

    result = await probe(
        target_for(ECR_NAME, provider="ecr", ca_bundle=certificates.ca_pem),
        login(),
        resolver=to_loopback(server.port),
        derivers={"ecr": deriver},
    )

    assert result.passed
    assert stages(result) == DERIVED_ORDER
    assert outcomes(result)[RegistryStage.CREDENTIAL_EXCHANGE] is StageOutcome.PASSED
    assert [(r.provider, r.address, r.username) for r in deriver.requests] == [
        ("ecr", ECR_NAME, USERNAME)
    ]
    assert [r.header("authorization") for r in server.requests] == [
        None,
        basic_header("AWS", "ecr-issued-password"),
    ]


@pytest.mark.parametrize(
    ("deriver_outcome", "fragment"),
    [
        (
            TokenExchangeFailed(TokenExchangeKind.DENIED, "The access key is valid but denied."),
            "valid but denied",
        ),
        (
            TokenExchangeFailed(TokenExchangeKind.CLOCK_SKEW, "The clock differs."),
            "clock differs",
        ),
        (RuntimeError(f"boom {SECRET}"), "could not be completed"),
        (None, "did not finish within"),
    ],
    ids=["denied", "clock-skew", "crash", "stalled"],
)
async def test_a_failed_exchange_fails_at_the_exchange_stage_and_connects_to_nothing(
    probe: Probe,
    servers: Serve,
    certificates: Certificates,
    deriver_outcome: Exception | None,
    fragment: str,
) -> None:
    server = await servers(registry_ok)

    result = await probe(
        target_for(ECR_NAME, provider="ecr", ca_bundle=certificates.ca_pem),
        login(),
        resolver=to_loopback(server.port),
        derivers={"ecr": ScriptedDeriver(deriver_outcome)},
    )

    assert result.failed_stage is RegistryStage.CREDENTIAL_EXCHANGE
    assert fragment in result.message
    assert SECRET not in repr(result)
    assert server.requests == []
    assert outcomes(result)[RegistryStage.DNS] is StageOutcome.NOT_RUN


async def test_a_derived_set_with_no_exchange_configured_fails_at_the_exchange_stage(
    probe: Probe,
) -> None:
    result = await probe(target_for(ECR_NAME, provider="ecr"), login(), derivers={})

    assert result.failed_stage is RegistryStage.CREDENTIAL_EXCHANGE
    assert "No credential exchange is configured" in result.message


async def test_an_exchange_that_issues_something_that_is_not_a_login_fails_the_exchange_stage(
    probe: Probe,
) -> None:
    unusable = DerivedSecret(SecretStr("not-a-basic-token"), EXPIRES)

    result = await probe(
        target_for(ECR_NAME, provider="ecr"), login(), derivers={"ecr": ScriptedDeriver(unusable)}
    )

    assert result.failed_stage is RegistryStage.CREDENTIAL_EXCHANGE


async def test_credentials_are_never_sent_when_certificate_verification_is_off(
    probe: Probe,
) -> None:
    with pytest.raises(ValueError, match="never sent without certificate verification"):
        await probe(target_for(f"{LOOPBACK}:5000", verify_tls=False), login())


async def test_a_provider_that_cannot_be_anonymous_is_not_tested_without_a_login(
    probe: Probe,
) -> None:
    with pytest.raises(ValueError, match="needs credentials"):
        await probe(target_for(ECR_NAME, provider="ecr"))


async def test_a_closed_coordinator_refuses_further_tests() -> None:
    coordinator = RegistryProbeCoordinator(derivers={})
    await coordinator.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.probe(target_for(f"{LOOPBACK}:5000"), None, key=None)


async def test_concurrent_tests_with_the_same_key_share_one_run_and_a_new_key_runs_again(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(registry_ok)
    coordinator = RegistryProbeCoordinator(derivers={}, timeouts=FAST)
    target = target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem)

    shared = await asyncio.gather(
        coordinator.probe(target, None, key=(1, "fingerprint")),
        coordinator.probe(target, None, key=(1, "fingerprint")),
    )
    after_shared = len(server.requests)
    await coordinator.probe(target, None, key=(1, "changed"))
    await coordinator.aclose()

    assert shared[0] is shared[1]
    assert after_shared == 1
    assert len(server.requests) == 2


async def test_cancelling_one_waiter_does_not_stop_the_shared_run(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(lambda request: registry_ok(request))
    coordinator = RegistryProbeCoordinator(derivers={}, timeouts=FAST)
    target = target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem)
    first = asyncio.create_task(coordinator.probe(target, None, key=(1, "f")))
    second = asyncio.create_task(coordinator.probe(target, None, key=(1, "f")))
    await asyncio.sleep(0)
    first.cancel()

    survivor = await second
    await coordinator.aclose()

    assert survivor.passed
    assert first.cancelled()


async def test_the_overall_limit_stops_a_test_whose_stages_each_fit_their_own_limit(
    probe: Probe, servers: Serve, certificates: Certificates
) -> None:
    registry = await servers(lambda request: Stall())
    tight = RegistryProbeTimeouts(registry_api=5.0, authentication=5.0, total=0.25)

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), timeouts=tight
    )

    assert result.failed_stage is RegistryStage.REGISTRY_API
    assert "overall time limit" in result.message


async def test_an_internal_registry_fails_at_tcp_naming_the_permit_and_is_never_dialled(
    probe: Probe, servers: Serve, certificates: Certificates, address_policy: Callable[..., None]
) -> None:
    server = await servers(registry_ok)
    address_policy()

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert result.failed_stage is RegistryStage.TCP
    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in result.message
    assert outcomes(result)[RegistryStage.TLS_HANDSHAKE] is StageOutcome.NOT_RUN
    assert server.requests == []


async def test_a_name_that_resolves_to_an_internal_address_fails_at_tcp(
    probe: Probe, address_policy: Callable[..., None]
) -> None:
    address_policy()

    result = await probe(target_for(f"{REGISTRY_NAME}:5000"), resolver=to_loopback())

    assert result.failed_stage is RegistryStage.TCP
    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in result.message


async def test_a_permitted_internal_registry_passes_every_stage(
    probe: Probe, servers: Serve, certificates: Certificates, address_policy: Callable[..., None]
) -> None:
    server = await servers(registry_ok)
    address_policy("127.0.0.0/8")

    result = await probe(target_for(f"{LOOPBACK}:{server.port}", ca_bundle=certificates.ca_pem))

    assert result.passed


async def test_a_token_service_on_an_internal_address_fails_authentication_and_receives_nothing(
    probe: Probe, servers: Serve, certificates: Certificates, address_policy: Callable[..., None]
) -> None:
    registry = await servers(bearer_registry("https://169.254.169.254/token"))
    address_policy("127.0.0.0/8")

    result = await probe(
        target_for(f"{LOOPBACK}:{registry.port}", ca_bundle=certificates.ca_pem), login()
    )

    assert result.failed_stage is RegistryStage.AUTHENTICATION
    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in result.message
    assert [request.header("authorization") for request in registry.requests] == [None]
