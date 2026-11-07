"""The catalogue download: what it accepts, and each refusal with a code and no remote text."""

import ssl
import zlib
from collections.abc import Awaitable, Callable

import pytest

from app import netpolicy
from app.services.template_fetch import (
    FAILURE_TEXT,
    FETCH_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    CatalogueFetcher,
    CatalogueFetchError,
    FetchedBody,
    decode_body,
    url_refusal,
)
from tests.notification_fixtures import certificates, receivers, trusting  # noqa: F401
from tests.notification_support import HttpReceiver
from tests.registry_client.loopback import Certificates
from tests.template_support import gzipped

type StartReceiver = Callable[..., Awaitable[HttpReceiver]]

REMOTE_TEXT = "remote-body-sentinel-text"
SMALL_LIMIT = 2048
SHORT_TIMEOUT = 0.3
SLOW_REPLY = 2.0
FOUND = 302
LINK_LOCAL_TARGET = "https://169.254.169.254/latest"


def fetcher(trust: ssl.SSLContext, **options: float) -> CatalogueFetcher:
    return CatalogueFetcher(trust=trust, **options)  # type: ignore[arg-type]


async def refusal_of(client: CatalogueFetcher, url: str) -> CatalogueFetchError:
    with pytest.raises(CatalogueFetchError) as refused:
        await client.fetch(url)
    return refused.value


def assert_no_remote_text(error: CatalogueFetchError) -> None:
    assert REMOTE_TEXT not in str(error) and str(error) == FAILURE_TEXT[error.code]


def test_the_product_limits_are_20_seconds_8_mib_and_3_redirects() -> None:
    assert (FETCH_TIMEOUT, MAX_BODY_BYTES, MAX_REDIRECTS) == (20.0, 8 * 1024 * 1024, 3)


async def test_a_document_over_https_is_returned_as_it_came(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(status=200, reply_body=b'{"a": 1}', identity=certificates.server)

    body = await fetcher(trusting).fetch(receiver.url("/doc"))

    assert body == FetchedBody(b'{"a": 1}', "")
    assert receiver.requests[0].method == "GET"
    assert receiver.requests[0].headers["accept-encoding"] == "gzip"


async def test_a_redirect_to_another_https_address_is_followed_and_the_final_document_returned(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    target = await receivers(status=200, reply_body=b"[]", identity=certificates.server)
    first = await receivers(status=FOUND, identity=certificates.server)
    first.headers = {"Location": target.url("/final")}

    body = await fetcher(trusting).fetch(first.url("/start"))

    assert body.data == b"[]"
    assert [len(first.requests), len(target.requests)] == [1, 1]
    assert target.requests[0].target == "/final"


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://catalogue.example.test/x", id="plain-http"),
        pytest.param("https://user:secret@catalogue.example.test/x", id="user-information"),
        pytest.param("https://user@catalogue.example.test/x", id="user-name-only"),
        pytest.param("https://catalogue.example.test/x#section", id="fragment"),
        pytest.param("ftp://catalogue.example.test/x", id="other-scheme"),
        pytest.param("https://", id="no-host"),
        pytest.param("https://catalogue.example.test/" + "a" * 2100, id="too-long"),
        pytest.param("https://catalogue.example.test/a b", id="whitespace"),
    ],
)
async def test_an_address_that_is_not_plain_https_is_refused_before_any_connection(
    url: str,
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    refused = await refusal_of(fetcher(trusting), url)

    assert url_refusal(url) is not None
    assert refused.code == "not_https"


async def test_a_redirect_to_plain_http_is_refused_without_connecting_to_it(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    plain = await receivers(status=200, reply_body=REMOTE_TEXT.encode())
    redirecting = await receivers(status=FOUND, identity=certificates.server)
    redirecting.headers = {"Location": plain.url("/x")}

    refused = await refusal_of(fetcher(trusting), redirecting.url("/"))

    assert refused.code == "not_https" and plain.requests == []
    assert_no_remote_text(refused)


async def test_a_redirect_to_an_internal_address_outside_the_permitted_networks_is_refused(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    redirecting = await receivers(status=FOUND, identity=certificates.server)
    redirecting.headers = {"Location": LINK_LOCAL_TARGET}

    refused = await refusal_of(fetcher(trusting), redirecting.url("/"))

    assert refused.code == "address_refused" and len(redirecting.requests) == 1
    assert_no_remote_text(refused)


async def test_an_internal_address_without_a_permit_is_refused_before_connecting(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(status=200, reply_body=b"[]", identity=certificates.server)
    netpolicy.configure(netpolicy.AddressPolicy())

    refused = await refusal_of(fetcher(trusting), receiver.url("/"))

    assert refused.code == "address_refused" and receiver.requests == []


async def test_more_than_three_redirects_are_refused_after_the_fourth_request(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    looping = await receivers(status=FOUND, identity=certificates.server)
    looping.headers = {"Location": looping.url("/again")}

    refused = await refusal_of(fetcher(trusting), looping.url("/"))

    assert refused.code == "too_many_redirects"
    assert len(looping.requests) == MAX_REDIRECTS + 1
    assert_no_remote_text(refused)


async def test_a_body_over_the_size_limit_is_refused_by_declared_length_and_by_count(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(
        status=200, reply_body=REMOTE_TEXT.encode() * 200, identity=certificates.server
    )

    refused = await refusal_of(fetcher(trusting, max_bytes=SMALL_LIMIT), receiver.url("/"))

    assert refused.code == "too_large"
    assert_no_remote_text(refused)


async def test_a_reply_that_stalls_past_the_timeout_is_refused(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(
        status=200, reply_body=b"[]", delay=SLOW_REPLY, identity=certificates.server
    )

    refused = await refusal_of(fetcher(trusting, timeout=SHORT_TIMEOUT), receiver.url("/"))

    assert refused.code == "timeout"


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        pytest.param(500, {}, "status", id="server-error"),
        pytest.param(404, {}, "status", id="not-found"),
        pytest.param(FOUND, {}, "status", id="redirect-without-location"),
        pytest.param(200, {"Content-Encoding": "br"}, "unsupported_encoding", id="brotli"),
    ],
)
async def test_a_reply_that_is_not_a_usable_document_is_refused_with_a_product_code(
    status: int,
    headers: dict[str, str],
    code: str,
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(
        status=status,
        reply_body=REMOTE_TEXT.encode(),
        headers=headers,
        identity=certificates.server,
    )

    refused = await refusal_of(fetcher(trusting), receiver.url("/"))

    assert refused.code == code
    assert_no_remote_text(refused)


async def test_a_certificate_the_client_does_not_trust_is_refused(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(status=200, reply_body=b"[]", identity=certificates.rogue_server)

    refused = await refusal_of(fetcher(trusting), receiver.url("/"))

    assert refused.code == "tls"


async def test_a_gzip_body_is_returned_encoded_and_decoded_within_the_limit(
    receivers: StartReceiver,  # noqa: F811
    certificates: Certificates,  # noqa: F811
    trusting: ssl.SSLContext,  # noqa: F811
) -> None:
    receiver = await receivers(
        status=200,
        reply_body=gzipped(b'{"a": 1}'),
        headers={"Content-Encoding": "gzip"},
        identity=certificates.server,
    )

    body = await fetcher(trusting).fetch(receiver.url("/"))

    assert body.encoding == "gzip" and decode_body(body) == b'{"a": 1}'


def test_a_compressed_body_that_expands_past_the_limit_is_refused_while_decoding() -> None:
    bomb = FetchedBody(gzipped(b"0" * 100_000), "gzip")

    with pytest.raises(CatalogueFetchError) as refused:
        decode_body(bomb, max_bytes=1000)

    assert refused.value.code == "too_large"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(FetchedBody(b"not gzip", "gzip"), id="not-gzip"),
        pytest.param(FetchedBody(gzipped(b"0" * 1000)[:-10], "gzip"), id="truncated"),
        pytest.param(FetchedBody(zlib.compress(b"x")[:-4], "deflate"), id="truncated-deflate"),
    ],
)
def test_a_compressed_body_that_is_damaged_is_refused_as_invalid(body: FetchedBody) -> None:
    with pytest.raises(CatalogueFetchError) as refused:
        decode_body(body)

    assert refused.value.code == "invalid_document"
