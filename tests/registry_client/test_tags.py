"""Listing a repository's tags: paging, Link validation, size limits, statuses and sign-in."""

import base64
import json
from collections.abc import Callable

import pytest
from pydantic import SecretStr

from app.registry_client.manifests import (
    ManifestCheckFailed,
    ManifestCheckKind,
    ManifestClient,
    ManifestTimeouts,
)
from app.registry_client.probe import RegistryLogin, RegistryTarget
from app.registry_client.providers import parse_extras
from app.registry_client.references import parse_reference
from app.registry_client.tags import MAX_PAGE_BYTES, MAX_PAGES, TagClient, TagListFailed
from tests.registry_client.conftest import Serve
from tests.registry_client.loopback import (
    LOOPBACK,
    Certificates,
    LoopbackServer,
    Request,
    Stall,
    closed_port,
    http_reply,
)

REPOSITORY = "org/app"
PATH = f"/v2/{REPOSITORY}/tags/list"
FIRST_PAGE = f"{PATH}?n=200"
FAST = ManifestTimeouts(dns=0.5, tcp=1.0, exchange=0.8, total=4.0)
STALLED = ManifestTimeouts(dns=0.5, tcp=1.0, exchange=0.3, total=1.0)
USERNAME, SECRET = "service-account", "tag-list-secret-marker-4410"
TOKEN = "issued-bearer-token-8812"
SERVICE = "registry.test"
KIND = ManifestCheckKind
NEXT_ON_FIRST = f'<{PATH}?last=a&n=200>; rel="next"'
LARGE_TAG_COUNT = 2400
LARGE_TAG = "t" * 100
CHUNK = b"x" * 1000
CHUNKS_OVER_THE_CAP = 300


def page(tags: list[str] | None, link: str | None = None) -> bytes:
    headers = {} if link is None else {"Link": link}
    return http_reply(200, json.dumps({"name": REPOSITORY, "tags": tags}).encode(), headers)


def next_link(query: str) -> str:
    return f'<{PATH}?{query}&n=200>; rel="next"'


def target_at(address: str, certificates: Certificates) -> RegistryTarget:
    return RegistryTarget("custom", address, True, certificates.ca_pem, parse_extras("custom", {}))


async def listing(
    server: LoopbackServer,
    certificates: Certificates,
    login: RegistryLogin | None = None,
    *,
    timeouts: ManifestTimeouts = FAST,
    reference: str = f"{REPOSITORY}:1",
) -> tuple[str, ...]:
    address = f"{LOOPBACK}:{server.port}"
    client = ManifestClient(timeouts=timeouts)
    try:
        return await TagClient(client).list_tags(
            target_at(address, certificates), parse_reference(f"{address}/{reference}"), login
        )
    finally:
        await client.aclose()


async def failure_kind(
    server: LoopbackServer,
    certificates: Certificates,
    login: RegistryLogin | None = None,
    *,
    timeouts: ManifestTimeouts = FAST,
) -> ManifestCheckKind:
    with pytest.raises(TagListFailed) as raised:
        await listing(server, certificates, login, timeouts=timeouts)
    return raised.value.kind


def the_listed_pages(request: Request) -> bytes:
    pages = {
        FIRST_PAGE: page(["a", "b"], next_link("last=b")),
        f"{PATH}?last=b&n=200": page(
            ["b", "c"],
            f'<{PATH}?last=c&n=200>; title="x, y"; rel="next", <{PATH}?n=200>; rel="first"',
        ),
        f"{PATH}?last=c&n=200": page(None),
    }
    return pages[request.target]


async def test_pages_are_followed_by_their_links_and_repeated_tags_are_listed_once(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(the_listed_pages)

    tags = await listing(server, certificates)

    assert tags == ("a", "b", "c")
    assert [r.target for r in server.requests] == [
        FIRST_PAGE,
        f"{PATH}?last=b&n=200",
        f"{PATH}?last=c&n=200",
    ]
    assert {r.method for r in server.requests} == {"GET"}


async def test_a_bearer_token_is_fetched_once_and_reused_on_every_page(
    servers: Serve, certificates: Certificates
) -> None:
    tokens = await servers(lambda r: http_reply(200, json.dumps({"token": TOKEN}).encode()))
    challenge = f'Bearer realm="https://{LOOPBACK}:{tokens.port}/token",service="{SERVICE}"'

    def registry(request: Request) -> bytes:
        if request.header("authorization") != f"Bearer {TOKEN}":
            return http_reply(401, b"", {"WWW-Authenticate": challenge})
        return the_listed_pages(request)

    server = await servers(registry)

    tags = await listing(server, certificates)

    assert tags == ("a", "b", "c")
    assert [r.header("authorization") for r in server.requests] == [None] + [f"Bearer {TOKEN}"] * 3
    assert len(tokens.requests) == 1
    assert "scope=repository%3Aorg%2Fapp%3Apull" in tokens.requests[0].target


async def test_basic_credentials_list_the_tags_and_their_absence_is_an_authentication_failure(
    servers: Serve, certificates: Certificates
) -> None:
    expected = "Basic " + base64.b64encode(f"{USERNAME}:{SECRET}".encode()).decode()

    def registry(request: Request) -> bytes:
        if request.header("authorization") != expected:
            return http_reply(401, b"", {"WWW-Authenticate": 'Basic realm="registry"'})
        return the_listed_pages(request)

    server = await servers(registry)

    tags = await listing(server, certificates, RegistryLogin(USERNAME, SecretStr(SECRET)))
    kind = await failure_kind(server, certificates)

    assert tags == ("a", "b", "c")
    assert kind is KIND.AUTHENTICATION


async def test_a_page_just_under_the_size_cap_is_read(
    servers: Serve, certificates: Certificates
) -> None:
    body = json.dumps({"tags": [LARGE_TAG] * LARGE_TAG_COUNT}).encode()
    assert MAX_PAGE_BYTES * 0.75 < len(body) < MAX_PAGE_BYTES
    server = await servers(lambda r: http_reply(200, body))

    tags = await listing(server, certificates)

    assert tags == (LARGE_TAG,)


def chunked_over_the_cap(request: Request) -> bytes:
    body = b"".join(
        f"{len(CHUNK):x}\r\n".encode() + CHUNK + b"\r\n" for _ in range(CHUNKS_OVER_THE_CAP)
    )
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    return head + body + b"0\r\n\r\n"


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(lambda r: http_reply(200, b"x" * (MAX_PAGE_BYTES + 1)), id="counted"),
        pytest.param(
            lambda r: http_reply(
                200, json.dumps({"tags": ["a"], "pad": "x" * MAX_PAGE_BYTES}).encode()
            ),
            id="padded",
        ),
        pytest.param(chunked_over_the_cap, id="chunked"),
    ],
)
async def test_a_page_over_the_size_cap_is_a_protocol_failure(
    servers: Serve, certificates: Certificates, handler: Callable[[Request], bytes]
) -> None:
    server = await servers(handler)

    assert await failure_kind(server, certificates) is KIND.PROTOCOL


def link_to(template: str, request: Request, other: LoopbackServer | None) -> str:
    host = request.header("host") or ""
    elsewhere = f"{LOOPBACK}:{other.port}" if other else ""
    return template.replace("{host}", host).replace("{other}", elsewhere)


@pytest.mark.parametrize(
    "template",
    [
        pytest.param(f'<https://{{other}}{FIRST_PAGE}>; rel="next"', id="other-server"),
        pytest.param(f'<https://localhost:1{FIRST_PAGE}>; rel="next"', id="other-host-name"),
        pytest.param(f'<http://{{host}}{FIRST_PAGE}>; rel="next"', id="plain-http"),
        pytest.param(f'<//{{host}}{FIRST_PAGE}>; rel="next"', id="scheme-relative"),
        pytest.param(f'<https://user:pw@{{host}}{FIRST_PAGE}>; rel="next"', id="user-info"),
        pytest.param(f'<https://{LOOPBACK}{FIRST_PAGE}>; rel="next"', id="other-port"),
        pytest.param(f'<https://evil.test{FIRST_PAGE}>; rel="next"', id="unrelated-host"),
        pytest.param('</v2/other/tags/list?n=200>; rel="next"', id="other-repository"),
        pytest.param(f'<{PATH}/../x?n=200>; rel="next"', id="dot-segments"),
        pytest.param('<tags/list?n=200>; rel="next"', id="relative-path"),
        pytest.param(f'<{FIRST_PAGE}#fragment>; rel="next"', id="fragment"),
        pytest.param(f'<{PATH}?a=%0d%0a>; rel="next"', id="encoded-line-break"),
        pytest.param(f'<{PATH}?n=200&x={"y" * 600}>; rel="next"', id="long-query"),
        pytest.param(f'<{PATH}>; rel="next"', id="no-query"),
        pytest.param(f'<{FIRST_PAGE}>; rel="next"', id="same-page-again"),
    ],
)
async def test_a_next_link_that_leaves_the_tag_list_is_refused_and_never_requested(
    servers: Serve, certificates: Certificates, template: str
) -> None:
    other = await servers(lambda r: page(["evil"]))
    server = await servers(lambda r: page(["a"], link_to(template, r, other)))

    kind = await failure_kind(server, certificates)

    assert kind is KIND.PROTOCOL
    assert other.requests == []
    assert len(server.requests) <= 2


async def test_an_absolute_link_to_the_same_host_and_port_is_followed(
    servers: Serve, certificates: Certificates
) -> None:
    def registry(request: Request) -> bytes:
        if "last" in request.target:
            return page(["z"])
        return page(
            ["y"], link_to(f'<https://{{host}}{PATH}?last=y&n=200>; rel="next"', request, None)
        )

    server = await servers(registry)

    assert await listing(server, certificates) == ("y", "z")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"", id="empty"),
        pytest.param(b"[]", id="array"),
        pytest.param(b'{"tags": "a"}', id="tags-a-string"),
        pytest.param(b'{"tags": [1]}', id="tag-a-number"),
        pytest.param(b'{"tags": [null]}', id="tag-null"),
        pytest.param(b'{"name": "x"}', id="no-tags-key"),
        pytest.param(b'{"tags": ["bad tag"]}', id="space-in-tag"),
        pytest.param(b'{"tags": ["a\\n"]}', id="line-break-in-tag"),
        pytest.param(b'{"tags": [""]}', id="empty-tag"),
        pytest.param(b'{"tags": ["-a"]}', id="leading-hyphen"),
        pytest.param(json.dumps({"tags": ["a" * 129]}).encode(), id="long-tag"),
        pytest.param(b'{"tags": ["a"]', id="truncated"),
        pytest.param(b"[" * 5000, id="deeply-nested"),
    ],
)
async def test_a_body_that_is_not_a_tag_list_is_a_protocol_failure(
    servers: Serve, certificates: Certificates, body: bytes
) -> None:
    server = await servers(lambda r: http_reply(200, body))

    assert await failure_kind(server, certificates) is KIND.PROTOCOL


async def test_an_empty_list_with_extra_keys_is_a_list_of_no_tags(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(lambda r: http_reply(200, b'{"tags": [], "extra": 1}'))

    assert await listing(server, certificates) == ()


async def test_a_listing_that_never_ends_stops_at_the_page_limit(
    servers: Serve, certificates: Certificates
) -> None:
    counter = iter(range(1, 1000))
    server = await servers(lambda r: page(["a"], next_link(f"last={next(counter)}")))

    kind = await failure_kind(server, certificates)

    assert kind is KIND.PROTOCOL
    assert len(server.requests) == MAX_PAGES


async def test_a_listing_of_exactly_the_page_limit_is_complete(
    servers: Serve, certificates: Certificates
) -> None:
    counter = iter(range(1, MAX_PAGES + 1))

    def registry(request: Request) -> bytes:
        number = next(counter)
        return page([f"v{number}"], None if number == MAX_PAGES else next_link(f"last={number}"))

    server = await servers(registry)

    tags = await listing(server, certificates)

    assert len(tags) == MAX_PAGES == len(server.requests)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        pytest.param(404, KIND.NOT_FOUND, id="not-found"),
        pytest.param(429, KIND.RATE_LIMITED, id="rate-limited"),
        pytest.param(403, KIND.AUTHENTICATION, id="forbidden"),
        pytest.param(500, KIND.PROTOCOL, id="server-error"),
        pytest.param(302, KIND.PROTOCOL, id="redirect-not-followed"),
    ],
)
async def test_a_status_maps_to_one_failure_kind_in_one_request(
    servers: Serve, certificates: Certificates, status: int, kind: ManifestCheckKind
) -> None:
    server = await servers(
        lambda r: http_reply(status, b"{}", {"Location": f"https://{LOOPBACK}:1/elsewhere"})
    )

    found = await failure_kind(server, certificates)

    assert found is kind
    assert len(server.requests) == 1


async def test_a_missing_repository_is_reported_in_the_products_words(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(lambda r: http_reply(404, b"{}"))

    with pytest.raises(TagListFailed) as raised:
        await listing(server, certificates)

    assert str(raised.value) == raised.value.message == "The registry has no such repository."
    assert isinstance(raised.value, ManifestCheckFailed)


@pytest.mark.parametrize(
    "link",
    [
        pytest.param(f'<{FIRST_PAGE}>; rel="prev"', id="previous"),
        pytest.param(f"<{FIRST_PAGE}>; rel=prev", id="bare-previous"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_link_header_without_a_next_relation_ends_the_list(
    servers: Serve, certificates: Certificates, link: str
) -> None:
    server = await servers(lambda r: page(["a"], link))

    assert await listing(server, certificates) == ("a",)


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("garbage", id="garbage"),
        pytest.param(f"{NEXT_ON_FIRST}, {next_link('last=b')}", id="two-next-links"),
        pytest.param(f"{NEXT_ON_FIRST} junk", id="trailing-junk"),
        pytest.param("x" * 5000, id="oversized-header"),
    ],
)
async def test_a_link_header_that_cannot_be_read_is_a_protocol_failure(
    servers: Serve, certificates: Certificates, link: str
) -> None:
    server = await servers(lambda r: page(["a"], link))

    assert await failure_kind(server, certificates) is KIND.PROTOCOL


async def test_the_relation_name_is_matched_without_regard_to_case_among_several(
    servers: Serve, certificates: Certificates
) -> None:
    def registry(request: Request) -> bytes:
        if "last" in request.target:
            return page(["b"])
        return page(["a"], f'<{PATH}?last=a&n=200>; REL="Next last"')

    server = await servers(registry)

    assert await listing(server, certificates) == ("a", "b")


async def test_a_registry_that_never_answers_times_out(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(lambda r: Stall())

    assert await failure_kind(server, certificates, timeouts=STALLED) is KIND.TIMEOUT


async def test_a_registry_that_is_not_listening_is_a_connection_failure(
    permit_loopback: None, certificates: Certificates
) -> None:
    address = f"{LOOPBACK}:{closed_port()}"
    client = ManifestClient(timeouts=FAST)

    with pytest.raises(TagListFailed) as raised:
        await TagClient(client).list_tags(
            target_at(address, certificates), parse_reference(f"{address}/{REPOSITORY}:1"), None
        )
    await client.aclose()

    assert raised.value.kind is KIND.TCP


async def test_an_internal_address_is_refused_before_any_connection(
    servers: Serve, certificates: Certificates, address_policy: Callable[..., None]
) -> None:
    server = await servers(the_listed_pages)
    address_policy()

    kind = await failure_kind(server, certificates)

    assert kind is KIND.ADDRESS_REFUSED
    assert server.requests == []


async def test_a_target_at_another_registry_than_the_reference_is_refused_before_connecting(
    servers: Serve, certificates: Certificates
) -> None:
    server = await servers(the_listed_pages)
    address = f"{LOOPBACK}:{server.port}"
    client = ManifestClient(timeouts=FAST)

    with pytest.raises(ValueError, match="not the registry"):
        await TagClient(client).list_tags(
            target_at(address, certificates), parse_reference(f"other.test/{REPOSITORY}:1"), None
        )
    await client.aclose()

    assert server.requests == []


async def test_credentials_are_never_sent_to_a_target_without_certificate_verification(
    servers: Serve,
) -> None:
    server = await servers(the_listed_pages)
    address = f"{LOOPBACK}:{server.port}"
    unverified = RegistryTarget("custom", address, False, None, parse_extras("custom", {}))
    client = ManifestClient(timeouts=FAST)

    with pytest.raises(ValueError, match="certificate verification"):
        await TagClient(client).list_tags(
            unverified,
            parse_reference(f"{address}/{REPOSITORY}:1"),
            RegistryLogin(USERNAME, SecretStr(SECRET)),
        )
    await client.aclose()

    assert server.requests == []


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param(REPOSITORY, id="untagged"),
        pytest.param(f"{REPOSITORY}@sha256:{'a' * 64}", id="pinned"),
    ],
)
async def test_the_tag_or_digest_of_the_reference_does_not_change_what_is_listed(
    servers: Serve, certificates: Certificates, reference: str
) -> None:
    server = await servers(lambda r: page(["a"]))

    tags = await listing(server, certificates, reference=reference)

    assert tags == ("a",)
    assert [r.target for r in server.requests] == [FIRST_PAGE]


async def test_a_closed_client_cannot_list(servers: Serve, certificates: Certificates) -> None:
    server = await servers(the_listed_pages)
    address = f"{LOOPBACK}:{server.port}"
    client = ManifestClient(timeouts=FAST)
    await client.aclose()

    with pytest.raises(RuntimeError):
        await TagClient(client).list_tags(
            target_at(address, certificates), parse_reference(f"{address}/{REPOSITORY}:1"), None
        )
