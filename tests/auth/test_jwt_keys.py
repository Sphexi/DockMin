"""Key sources for platform tokens: the JWKS fetch refusals and the refresh and retention rules."""

import asyncio
import gzip
import json
import ssl
from collections.abc import Awaitable, Callable

import pytest

from app import netpolicy
from app.auth.jwt_key_documents import (
    KeyDocumentError,
    VerificationKey,
    parse_key_document,
)
from app.auth.jwt_keys import (
    MAX_KEY_AGE,
    MIN_REFRESH_INTERVAL,
    REFRESH_AFTER,
    KeyFetchError,
    KeyRefused,
    KeyStore,
)
from app.registry_client.tls import registry_context
from app.services.jwks_fetcher import FETCH_TIMEOUT, MAX_DOCUMENT_BYTES, JwksFetcher
from tests.jwt_support import KID, OTHER_KID, Signer, ec_signer, jwks_body, public_pem
from tests.notification_support import HttpReceiver
from tests.registry_client.loopback import Certificates

type StartReceiver = Callable[..., Awaitable[HttpReceiver]]

SHORT_TIMEOUT = 0.3
SLOW_REPLY = 2.0
TOO_BIG = MAX_DOCUMENT_BYTES + 1
FOUND = 302
SERVER_ERROR = 500


class ScriptedFetcher:
    """A fetcher that returns or raises what the test queues, and counts its calls."""

    def __init__(self, *outcomes: list[VerificationKey] | KeyFetchError) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def fetch(self) -> list[VerificationKey]:
        self.calls += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, KeyFetchError):
            raise outcome
        return outcome


class Ticker:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def signer() -> Signer:
    return ec_signer()


async def _fetch(
    receiver: HttpReceiver, trust: ssl.SSLContext | None, within: float = FETCH_TIMEOUT
) -> list[VerificationKey]:
    fetcher = JwksFetcher(receiver.url("/jwks"), ["ES256"], trust=trust, timeout=within)
    return await fetcher.fetch()


async def test_a_jwks_document_over_https_yields_its_keys(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    signer: Signer,
) -> None:
    receiver = await receivers(
        status=200, reply_body=jwks_body(signer), identity=certificates.server
    )

    keys = await _fetch(receiver, trusting)

    assert [key.kid for key in keys] == [KID]
    assert receiver.requests[0].method == "GET"


async def test_a_plain_http_url_is_refused_before_any_connection(
    receivers: StartReceiver, signer: Signer
) -> None:
    receiver = await receivers(status=200, reply_body=jwks_body(signer))

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, None)

    assert (refused.value.reason, receiver.requests) == ("not_https", [])


async def test_a_redirect_is_not_followed(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
) -> None:
    receiver = await receivers(
        status=FOUND, headers={"Location": "https://127.0.0.1:9/jwks"}, identity=certificates.server
    )

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting)

    assert (refused.value.reason, len(receiver.requests)) == ("redirect", 1)


async def test_an_internal_address_without_a_permit_is_refused_before_connecting(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    signer: Signer,
) -> None:
    receiver = await receivers(
        status=200, reply_body=jwks_body(signer), identity=certificates.server
    )
    netpolicy.configure(netpolicy.AddressPolicy())

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting)

    assert (refused.value.reason, receiver.requests) == ("address_refused", [])


async def test_a_body_over_the_size_limit_is_refused(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
) -> None:
    receiver = await receivers(status=200, reply_body=b"x" * TOO_BIG, identity=certificates.server)

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting)

    assert refused.value.reason == "too_large"


async def test_a_compressed_reply_is_refused_and_identity_encoding_is_requested(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    signer: Signer,
) -> None:
    receiver = await receivers(
        status=200,
        reply_body=gzip.compress(jwks_body(signer)),
        headers={"Content-Encoding": "gzip"},
        identity=certificates.server,
    )

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting)

    assert refused.value.reason == "content_encoding"
    assert receiver.requests[0].headers["accept-encoding"] == "identity"


async def test_a_reply_that_takes_longer_than_the_timeout_is_refused(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    signer: Signer,
) -> None:
    receiver = await receivers(
        status=200, reply_body=jwks_body(signer), delay=SLOW_REPLY, identity=certificates.server
    )

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting, within=SHORT_TIMEOUT)

    assert refused.value.reason == "timeout"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        pytest.param(SERVER_ERROR, b"", "status", id="server-error"),
        pytest.param(200, b"not json", "invalid_document", id="not-json"),
        pytest.param(200, b'{"keys": []}', "no_usable_keys", id="no-keys"),
    ],
)
async def test_a_reply_that_is_not_a_usable_key_set_is_refused(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    status: int,
    body: bytes,
    reason: str,
) -> None:
    receiver = await receivers(status=status, reply_body=body, identity=certificates.server)

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trusting)

    assert refused.value.reason == reason


async def test_a_certificate_the_client_does_not_trust_is_refused(
    receivers: StartReceiver,
    certificates: Certificates,
    signer: Signer,
) -> None:
    receiver = await receivers(
        status=200, reply_body=jwks_body(signer), identity=certificates.rogue_server
    )
    trust = registry_context(verify_tls=True, ca_bundle=certificates.ca_pem)

    with pytest.raises(KeyFetchError) as refused:
        await _fetch(receiver, trust)

    assert refused.value.reason == "tls"


def test_a_jwks_document_keeps_only_public_signing_keys_within_the_allow_list(
    signer: Signer,
) -> None:
    good = signer.jwk()
    encryption = {**ec_signer(OTHER_KID).jwk(), "use": "enc"}
    wrong_alg = {**ec_signer("rs").jwk(), "alg": "ES384"}
    symmetric = {"kty": "oct", "k": "AAAA", "kid": "shared", "use": "sig"}
    private = {
        **good,
        "kid": "private",
        "d": "AAAA",
    }
    document = json.dumps({"keys": [good, encryption, wrong_alg, symmetric, private, "x"]})

    keys = parse_key_document(document.encode(), ["ES256"])

    assert [key.kid for key in keys] == [KID]


def test_a_pem_public_key_is_one_key_without_an_id(signer: Signer) -> None:
    [key] = parse_key_document(public_pem(signer), ["ES256"])

    assert key.kid is None
    assert key.fits("ES256") and not key.fits("RS256")


@pytest.mark.parametrize("document", [b"", b"{}", b'{"keys": 3}', b"-----BEGIN NOTHING-----"])
def test_a_document_that_is_neither_pem_nor_jwks_is_refused(document: bytes) -> None:
    with pytest.raises(KeyDocumentError):
        parse_key_document(document, ["ES256"])


async def test_an_unknown_key_id_refreshes_the_set_at_most_once_a_minute(signer: Signer) -> None:
    clock = Ticker()
    newcomer = ec_signer(OTHER_KID)
    fetcher = ScriptedFetcher([signer.verification_key()], [newcomer.verification_key()])
    store = KeyStore(fetcher=fetcher, monotonic=clock)
    await store.select(KID)
    clock.now += MIN_REFRESH_INTERVAL + 1

    for _ in range(3):
        with pytest.raises(KeyRefused):
            await store.select("rotated")
    calls_within_the_minute = fetcher.calls
    clock.now += MIN_REFRESH_INTERVAL + 1
    with pytest.raises(KeyRefused):
        await store.select("rotated")

    assert (calls_within_the_minute, fetcher.calls) == (2, 3)


async def test_concurrent_lookups_of_an_unknown_key_id_share_one_fetch(signer: Signer) -> None:
    clock = Ticker()
    fetcher = ScriptedFetcher([signer.verification_key()])
    store = KeyStore(fetcher=fetcher, monotonic=clock)
    await store.select(KID)
    clock.now += MIN_REFRESH_INTERVAL + 1

    results = await asyncio.gather(
        *(store.select("rotated") for _ in range(5)), return_exceptions=True
    )

    assert fetcher.calls == 2
    assert all(isinstance(result, KeyRefused) for result in results)


async def test_a_key_published_after_rotation_is_picked_up_by_the_request_that_names_it(
    signer: Signer,
) -> None:
    clock = Ticker()
    newcomer = ec_signer(OTHER_KID)
    fetcher = ScriptedFetcher(
        [signer.verification_key()], [signer.verification_key(), newcomer.verification_key()]
    )
    store = KeyStore(fetcher=fetcher, monotonic=clock)
    await store.select(KID)
    clock.now += MIN_REFRESH_INTERVAL + 1

    chosen = await store.select(OTHER_KID)

    assert chosen.kid == OTHER_KID


async def test_a_failed_refresh_keeps_the_last_good_set_for_a_day_and_then_refuses(
    signer: Signer,
) -> None:
    clock = Ticker()
    fetcher = ScriptedFetcher([signer.verification_key()], KeyFetchError("network"))
    store = KeyStore(fetcher=fetcher, monotonic=clock)
    await store.select(KID)

    clock.now += REFRESH_AFTER + 1
    still_served = await store.select(KID)
    clock.now += MAX_KEY_AGE - REFRESH_AFTER
    with pytest.raises(KeyRefused) as refused:
        await store.select(KID)

    assert still_served.kid == KID
    assert refused.value.reason == "keys_unavailable"


async def test_a_set_that_has_never_been_fetched_refuses_and_tries_again_after_a_minute(
    signer: Signer,
) -> None:
    clock = Ticker()
    fetcher = ScriptedFetcher(KeyFetchError("network"), [signer.verification_key()])
    store = KeyStore(fetcher=fetcher, monotonic=clock)

    with pytest.raises(KeyRefused):
        await store.select(KID)
    with pytest.raises(KeyRefused):
        await store.select(KID)
    clock.now += MIN_REFRESH_INTERVAL + 1
    chosen = await store.select(KID)

    assert (fetcher.calls, chosen.kid) == (2, KID)


async def test_a_set_older_than_an_hour_is_refreshed_at_the_next_verification(
    signer: Signer,
) -> None:
    clock = Ticker()
    fetcher = ScriptedFetcher([signer.verification_key()])
    store = KeyStore(fetcher=fetcher, monotonic=clock)
    await store.select(KID)

    await store.select(KID)
    clock.now += REFRESH_AFTER + 1
    await store.select(KID)

    assert fetcher.calls == 2


async def test_a_store_over_a_real_fetch_serves_the_published_keys_and_follows_a_rotation(
    receivers: StartReceiver,
    certificates: Certificates,
    trusting: ssl.SSLContext,
    signer: Signer,
) -> None:
    clock = Ticker()
    newcomer = ec_signer(OTHER_KID)
    receiver = await receivers(
        status=200, reply_body=jwks_body(signer), identity=certificates.server
    )
    store = KeyStore(
        fetcher=JwksFetcher(receiver.url("/jwks"), ["ES256"], trust=trusting), monotonic=clock
    )
    first = await store.select(KID)
    receiver.reply_body = jwks_body(signer, newcomer)
    clock.now += MIN_REFRESH_INTERVAL + 1

    second = await store.select(OTHER_KID)

    assert (first.kid, second.kid, len(receiver.requests)) == (KID, OTHER_KID, 2)
