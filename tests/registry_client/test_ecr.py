"""The ECR token exchange over a mocked AWS endpoint, with a controlled clock."""

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app import netpolicy
from app.registry_client.ecr import (
    MAX_LIFETIME,
    MAX_RESPONSE_BYTES,
    DerivationRequest,
    EcrTokenSource,
    ecr_api_host,
    new_ecr_http_client,
)
from app.registry_client.errors import TokenExchangeFailed, TokenExchangeKind
from app.registry_client.providers import parse_extras
from tests.registry_client.signing_data import ORACLE

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TOKEN_MARKER = "ecr-password-marker-5521"
TOKEN = base64.b64encode(f"AWS:{TOKEN_MARKER}".encode()).decode()
TARGET = "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken"
PAST_SKEW = timedelta(minutes=10)
WITHIN_SKEW = timedelta(seconds=3)

Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def success_body(*, expires: datetime = NOW + timedelta(hours=12), token: str = TOKEN) -> bytes:
    document = {
        "authorizationData": [
            {
                "authorizationToken": token,
                "expiresAt": expires.timestamp(),
                "proxyEndpoint": "https://123456789012.dkr.ecr.eu-west-2.amazonaws.com",
            }
        ]
    }
    return json.dumps(document).encode()


def derivation(region: str = "eu-west-2", username: str = ACCESS_KEY) -> DerivationRequest:
    return DerivationRequest(
        provider="ecr",
        address=f"123456789012.dkr.ecr.{region}.amazonaws.com",
        username=username,
        secret=SecretStr(SECRET_KEY),
        extras=parse_extras("ecr", {"region": region}),
    )


def source_over(handler: Handler) -> EcrTokenSource:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False
    )
    return EcrTokenSource(client, lambda: NOW)


def answering(response: httpx.Response) -> Handler:
    async def handle(request: httpx.Request) -> httpx.Response:
        return response

    return handle


def aws_error(status: int, error_type: str, *, date: datetime | None = None) -> httpx.Response:
    headers = {"date": format_datetime(date, usegmt=True)} if date else {}
    body = {"__type": f"com.amazon.coral.service#{error_type}", "message": f"leak {SECRET_KEY}"}
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers)


async def derive_failure(handler: Handler, request: DerivationRequest) -> TokenExchangeFailed:
    source = source_over(handler)
    with pytest.raises(TokenExchangeFailed) as failure:
        await source.derive(request)
    return failure.value


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
def recording(seen: list[httpx.Request]) -> Handler:
    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=success_body())

    return handle


async def test_a_successful_exchange_returns_the_token_and_its_expiry(recording: Handler) -> None:
    source = source_over(recording)

    derived = await source.derive(derivation())

    assert derived.basic_token.get_secret_value() == TOKEN
    assert derived.expires_at == NOW + timedelta(hours=12)


async def test_the_request_is_a_signed_post_to_the_regional_endpoint_with_no_secret_in_it(
    recording: Handler, seen: list[httpx.Request]
) -> None:
    source = source_over(recording)
    case = next(c for c in ORACLE["cases"] if c["region"] == "eu-west-2")

    await source.derive(derivation())

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.ecr.eu-west-2.amazonaws.com/"
    assert request.content == b"{}"
    assert request.headers["x-amz-target"] == TARGET
    assert request.headers["content-type"] == "application/x-amz-json-1.1"
    assert request.headers["host"] == "api.ecr.eu-west-2.amazonaws.com"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["authorization"] == case["authorization"]
    assert SECRET_KEY not in repr(request.headers) + str(request.url) + request.content.decode()


@pytest.mark.parametrize(
    ("region", "host"),
    [
        ("us-east-1", "api.ecr.us-east-1.amazonaws.com"),
        ("us-gov-west-1", "api.ecr.us-gov-west-1.amazonaws.com"),
        ("cn-north-1", "api.ecr.cn-north-1.amazonaws.com.cn"),
        ("cn-northwest-1", "api.ecr.cn-northwest-1.amazonaws.com.cn"),
    ],
)
def test_the_api_host_follows_the_region_including_the_china_partition(
    region: str, host: str
) -> None:
    assert ecr_api_host(region) == host


@pytest.mark.parametrize(
    "region",
    ["", "us-east", "evil.example.com/#", "us-east-1.attacker.example", "a" * 40, "US-EAST-1"],
)
def test_a_region_that_is_not_an_aws_region_never_becomes_a_host(region: str) -> None:
    with pytest.raises(ValueError, match="not an AWS region"):
        ecr_api_host(region)


async def test_the_china_partition_request_goes_to_its_own_host(
    recording: Handler, seen: list[httpx.Request]
) -> None:
    source = source_over(recording)

    await source.derive(derivation("cn-north-1"))

    assert seen[0].url.host == "api.ecr.cn-north-1.amazonaws.com.cn"


async def test_an_expiry_beyond_twelve_hours_is_clamped(recording: Handler) -> None:
    handler = answering(httpx.Response(200, content=success_body(expires=NOW + timedelta(days=30))))
    source = source_over(handler)

    derived = await source.derive(derivation())

    assert derived.expires_at == NOW + MAX_LIFETIME


async def test_a_shorter_expiry_is_kept() -> None:
    expires = NOW + timedelta(minutes=30)
    source = source_over(answering(httpx.Response(200, content=success_body(expires=expires))))

    derived = await source.derive(derivation())

    assert derived.expires_at == expires


async def test_the_derived_secret_hides_the_token_in_its_repr() -> None:
    source = source_over(answering(httpx.Response(200, content=success_body())))

    derived = await source.derive(derivation())

    assert TOKEN not in repr(derived)
    assert TOKEN_MARKER not in repr(derived)


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (aws_error(403, "InvalidSignatureException", date=NOW + WITHIN_SKEW), "refused_key"),
        (aws_error(400, "UnrecognizedClientException"), "refused_key"),
        (aws_error(403, "AccessDeniedException"), "denied"),
        (aws_error(400, "AccessDenied"), "denied"),
        (aws_error(403, "InvalidSignatureException", date=NOW + PAST_SKEW), "clock_skew"),
        (aws_error(400, "RequestExpired"), "clock_skew"),
        (aws_error(400, "RequestTimeTooSkewed"), "clock_skew"),
        (aws_error(400, "ThrottlingException"), "unreachable"),
        (aws_error(429, "TooManyRequestsException"), "unreachable"),
        (aws_error(503, "ServiceUnavailableException"), "unreachable"),
        (httpx.Response(500, content=b"boom"), "unreachable"),
        (httpx.Response(403, content=b""), "refused_key"),
        (httpx.Response(418, content=b"teapot"), "malformed"),
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "malformed"),
    ],
    ids=[
        "bad-signature-clock-fine",
        "unrecognised-key",
        "access-denied",
        "access-denied-plain",
        "bad-signature-clock-skewed",
        "request-expired",
        "time-too-skewed",
        "throttled",
        "too-many-requests",
        "service-unavailable",
        "server-error",
        "bare-403",
        "unexpected-status",
        "redirect-not-followed",
    ],
)
async def test_an_aws_error_is_mapped_to_a_failure_kind_without_the_aws_text(
    response: httpx.Response, kind: str
) -> None:
    failure = await derive_failure(answering(response), derivation())

    assert failure.kind is TokenExchangeKind(kind)
    assert SECRET_KEY not in f"{failure.message}{failure!r}{failure.args!r}"


async def test_the_error_type_header_is_honoured_when_the_body_is_empty() -> None:
    response = httpx.Response(
        400, headers={"x-amzn-ErrorType": "AccessDeniedException:http://internal/coral/"}
    )

    failure = await derive_failure(answering(response), derivation())

    assert failure.kind is TokenExchangeKind.DENIED


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, content=b'{"authorizationData": []}'),
        httpx.Response(200, content=b'{"authorizationData": [{"expiresAt": 1}]}'),
        httpx.Response(200, content=b"[" * 50_000),
        httpx.Response(200, content=success_body(token="not base64!")),
        httpx.Response(200, content=success_body(token=base64.b64encode(b"nocolon").decode())),
        httpx.Response(200, content=success_body(token=base64.b64encode(b"x" * 9000).decode())),
        httpx.Response(200, content=success_body(expires=NOW - timedelta(hours=1))),
        httpx.Response(200, content=success_body(expires=NOW)),
        httpx.Response(
            200,
            content=b'{"authorizationData":[{"authorizationToken":"%s","expiresAt":1e999}]}'
            % TOKEN.encode(),
        ),
        httpx.Response(
            200,
            content=b'{"authorizationData":[{"authorizationToken":"%s","expiresAt":true}]}'
            % TOKEN.encode(),
        ),
        httpx.Response(
            200,
            content=b'{"authorizationData":[{"authorizationToken":"%s","expiresAt":"soon"}]}'
            % TOKEN.encode(),
        ),
        httpx.Response(200, content=b"x", headers={"content-encoding": "gzip"}),
    ],
    ids=[
        "not-json",
        "empty-authorization-data",
        "missing-token",
        "deeply-nested",
        "token-not-base64",
        "token-without-separator",
        "token-too-long",
        "already-expired",
        "expires-now",
        "non-finite-expiry",
        "boolean-expiry",
        "string-expiry",
        "compressed",
    ],
)
async def test_a_success_reply_that_is_not_a_usable_token_is_malformed(
    response: httpx.Response,
) -> None:
    failure = await derive_failure(answering(response), derivation())

    assert failure.kind is TokenExchangeKind.MALFORMED


async def test_a_declared_oversized_reply_is_refused_as_oversized() -> None:
    response = httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    failure = await derive_failure(answering(response), derivation())

    assert failure.kind is TokenExchangeKind.OVERSIZED


async def test_an_oversized_reply_streamed_without_a_length_is_cut_off() -> None:
    async def stream(request: httpx.Request) -> httpx.Response:
        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(MAX_RESPONSE_BYTES // 8192 + 4):
                yield b"y" * 8192

        return httpx.Response(200, content=chunks())

    failure = await derive_failure(stream, derivation())

    assert failure.kind is TokenExchangeKind.OVERSIZED


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout, httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError],
)
async def test_a_transport_error_is_unreachable_and_its_text_is_not_kept(
    error: type[httpx.RequestError],
) -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise error(f"transport detail {SECRET_KEY}", request=request)

    failure = await derive_failure(fail, derivation())

    assert failure.kind is TokenExchangeKind.UNREACHABLE
    assert failure.__cause__ is None
    assert failure.__context__ is None
    assert SECRET_KEY not in f"{failure.message}{failure!r}"


@pytest.mark.parametrize("username", ["AKIA bad\r\nX: y", "short", "akiaiosfodnn7example", ""])
async def test_an_access_key_that_could_not_be_valid_is_refused_before_anything_is_sent(
    username: str, seen: list[httpx.Request], recording: Handler
) -> None:
    failure = await derive_failure(recording, derivation(username=username))

    assert failure.kind is TokenExchangeKind.REFUSED_KEY
    assert seen == []


async def test_extras_of_another_provider_are_a_programming_error() -> None:
    source = source_over(answering(httpx.Response(200)))
    request = DerivationRequest(
        provider="ecr",
        address="registry.example.com",
        username=ACCESS_KEY,
        secret=SecretStr(SECRET_KEY),
        extras=parse_extras("custom", {}),
    )

    with pytest.raises(TypeError):
        await source.derive(request)


@pytest.mark.parametrize("flags", [{"trust_env": True}, {"follow_redirects": True}])
async def test_a_client_that_reads_the_environment_or_follows_redirects_is_refused(
    flags: dict[str, bool],
) -> None:
    options: dict[str, Any] = {"trust_env": False, "follow_redirects": False, **flags}
    client = httpx.AsyncClient(**options)

    with pytest.raises(ValueError, match="ignore the environment"):
        EcrTokenSource(client, lambda: NOW)
    await client.aclose()


async def test_the_production_client_ignores_the_environment_and_never_follows_redirects() -> None:
    client = new_ecr_http_client()

    flags = (client.trust_env, client.follow_redirects)
    await client.aclose()

    assert flags == (False, False)


async def test_nothing_secret_reaches_a_log_record_on_success_or_failure(
    caplog: pytest.LogCaptureFixture, recording: Handler
) -> None:
    caplog.set_level(logging.DEBUG)
    good = source_over(recording)

    await good.derive(derivation())
    await derive_failure(answering(aws_error(403, "InvalidSignatureException")), derivation())

    text = " ".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert [m for m in (SECRET_KEY, TOKEN, TOKEN_MARKER) if m in text] == []


async def test_a_refused_address_is_its_own_failure_kind_not_unreachable() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise netpolicy.AddressRefused("169.254.169.254")

    failure = await derive_failure(refuse, derivation())

    assert failure.kind is TokenExchangeKind.ADDRESS_REFUSED
    assert failure.message == netpolicy.REFUSAL_TEXT
    assert "169.254.169.254" not in f"{failure.message}{failure!r}"


async def test_the_production_client_refuses_a_regional_host_that_resolves_to_an_internal_address(
    fake_hosts: dict[str, str],
) -> None:
    fake_hosts["api.ecr.eu-west-2.amazonaws.com"] = "169.254.169.254"
    client = new_ecr_http_client()
    source = EcrTokenSource(client, lambda: NOW)

    with pytest.raises(TokenExchangeFailed) as failure:
        await source.derive(derivation())
    await client.aclose()

    assert failure.value.kind is TokenExchangeKind.ADDRESS_REFUSED
