"""The DNS, TCP and TLS primitives shared by the staged connection tests."""

import asyncio
import errno
import ipaddress
import socket
import ssl
import threading

import pytest

from app.netcheck import (
    MAX_RESOLVED_ADDRESSES,
    MAX_TCP_ATTEMPTS,
    BoundedResolver,
    CertificateFault,
    TlsFailure,
    classify_certificate_fault,
    classify_tls_failure,
    clean_text,
    connect_first,
    dns_failure_message,
    ip_literal,
    literal_address,
    seconds_text,
    tcp_failure_kind,
    tcp_failure_message,
    usable_addresses,
)
from tests.registry_client.loopback import LOOPBACK, closed_port

WAIT_SECONDS = 5.0
UNBOUND = 0.0
ATTEMPT_LIMIT = 0.5


def tls_error(reason: str, cls: type[ssl.SSLError] = ssl.SSLError) -> ssl.SSLError:
    error = cls(1, f"[SSL] {reason}")
    error.reason = reason
    return error


@pytest.mark.parametrize(
    ("code", "fault"),
    [
        (2, CertificateFault.UNKNOWN_ISSUER),
        (18, CertificateFault.UNKNOWN_ISSUER),
        (19, CertificateFault.UNKNOWN_ISSUER),
        (20, CertificateFault.UNKNOWN_ISSUER),
        (21, CertificateFault.UNKNOWN_ISSUER),
        (10, CertificateFault.EXPIRED),
        (9, CertificateFault.NOT_YET_VALID),
        (62, CertificateFault.NAME_MISMATCH),
        (64, CertificateFault.NAME_MISMATCH),
        (26, CertificateFault.WRONG_PURPOSE),
        (65, CertificateFault.WEAK_KEY),
        (66, CertificateFault.WEAK_KEY),
        (67, CertificateFault.WEAK_KEY),
        (0, CertificateFault.OTHER),
        (999, CertificateFault.OTHER),
    ],
)
def test_a_certificate_verify_code_is_classified(code: int, fault: CertificateFault) -> None:
    assert classify_certificate_fault(code) is fault


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (tls_error("WRONG_VERSION_NUMBER"), TlsFailure.NOT_TLS),
        (tls_error("HTTP_REQUEST"), TlsFailure.NOT_TLS),
        (tls_error("UNKNOWN_PROTOCOL"), TlsFailure.NOT_TLS),
        (tls_error("UNSUPPORTED_PROTOCOL"), TlsFailure.PROTOCOL_VERSION),
        (tls_error("TLSV1_ALERT_PROTOCOL_VERSION"), TlsFailure.PROTOCOL_VERSION),
        (tls_error("TLSV1_ALERT_UNKNOWN_CA"), TlsFailure.CERTIFICATE_ALERT),
        (tls_error("SSLV3_ALERT_BAD_CERTIFICATE"), TlsFailure.CERTIFICATE_ALERT),
        (tls_error("SSLV3_ALERT_HANDSHAKE_FAILURE"), TlsFailure.HANDSHAKE_REJECTED),
        (tls_error("NO_SHARED_CIPHER"), TlsFailure.HANDSHAKE_REJECTED),
        (tls_error("TLSV1_ALERT_INTERNAL_ERROR"), TlsFailure.ALERT),
        (tls_error("EOF occurred in violation of protocol", ssl.SSLEOFError), TlsFailure.CLOSED),
        (tls_error("SOMETHING_ELSE"), TlsFailure.OTHER),
    ],
)
def test_a_handshake_failure_is_classified_by_its_reason(
    error: ssl.SSLError, failure: TlsFailure
) -> None:
    assert classify_tls_failure(error) is failure


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        (errno.ECONNREFUSED, "refused"),
        (errno.ENETUNREACH, "unreachable"),
        (errno.EHOSTUNREACH, "unreachable"),
        (errno.EACCES, "blocked"),
        (errno.EPERM, "blocked"),
        (errno.ETIMEDOUT, "timeout"),
        (errno.EIO, "other"),
        (None, "other"),
    ],
)
def test_a_connect_error_number_is_classified(code: int | None, kind: str) -> None:
    assert tcp_failure_kind(code) == kind


@pytest.mark.parametrize(
    ("kinds", "fragment"),
    [
        (("timeout", "refused"), "refused"),
        (("timeout", "unreachable"), "no route"),
        (("other", "blocked"), "network policy"),
        (("timeout",), "timed out after 5 seconds"),
        (("overall",), "overall time limit of 30 seconds"),
        (("other",), "could not be established"),
        ((), "could not be established"),
    ],
)
def test_the_most_telling_tcp_failure_names_the_message(
    kinds: tuple[str, ...], fragment: str
) -> None:
    message = tcp_failure_message(kinds, tcp_timeout=5, total_timeout=30)

    assert fragment in message


@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (socket.EAI_NONAME, "does not exist"),
        (socket.EAI_AGAIN, "did not answer"),
        (socket.EAI_FAIL, "permanent failure"),
        (None, "name lookup failed"),
        (12345, "name lookup failed"),
    ],
)
def test_a_resolver_error_code_is_explained(code: int | None, fragment: str) -> None:
    assert fragment in dns_failure_message(code)


@pytest.mark.parametrize(
    ("seconds", "text"), [(1, "1 second"), (0.5, "0.5 seconds"), (5, "5 seconds")]
)
def test_durations_are_written_for_a_message(seconds: float, text: str) -> None:
    assert seconds_text(seconds) == text


def test_text_from_a_peer_is_cut_and_stripped_of_control_and_format_characters() -> None:
    hostile = f"ok{chr(0x1B)}{chr(0x200B)}{chr(0x2028)}fine"

    assert clean_text(hostile) == "okfine"
    assert clean_text("x" * 200) == "x" * 64
    assert clean_text("x" * 200, 5) == "xxxxx"


def test_an_ip_literal_is_recognised_and_a_name_is_not() -> None:
    assert ip_literal(LOOPBACK) == ipaddress.ip_address(LOOPBACK)
    assert ip_literal("::1") == ipaddress.ip_address("::1")
    assert ip_literal("registry.example.com") is None


def test_literal_addresses_are_built_in_their_connectable_form() -> None:
    assert literal_address(ipaddress.ip_address(LOOPBACK), 5000) == (
        socket.AF_INET,
        (LOOPBACK, 5000),
    )
    assert literal_address(ipaddress.ip_address("::1"), 5000) == (
        socket.AF_INET6,
        ("::1", 5000, 0, 0),
    )


def info(family: int, host: str, port: int = 443) -> tuple[int, int, int, str, tuple[object, ...]]:
    return (family, socket.SOCK_STREAM, 6, "", (host, port))


def test_resolved_addresses_keep_only_distinct_internet_stream_addresses() -> None:
    infos = [
        info(socket.AF_INET, "192.0.2.1"),
        info(socket.AF_INET, "192.0.2.1"),
        info(socket.AF_INET6, "2001:db8::1"),
        info(socket.AF_UNIX, "/tmp/x"),
    ]

    assert usable_addresses(infos) == [
        (socket.AF_INET, ("192.0.2.1", 443)),
        (socket.AF_INET6, ("2001:db8::1", 443)),
    ]


def test_a_lookup_that_returns_a_flood_of_addresses_is_cut_to_the_cap() -> None:
    infos = [info(socket.AF_INET, f"10.0.{i // 256}.{i % 256}") for i in range(300)]

    assert len(usable_addresses(infos)) == MAX_RESOLVED_ADDRESSES


@pytest.mark.usefixtures("permit_loopback")
async def test_connecting_stops_at_the_first_address_that_answers() -> None:
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    listener.listen(1)
    refused = (socket.AF_INET, (LOOPBACK, closed_port()))
    open_port = (socket.AF_INET, (LOOPBACK, listener.getsockname()[1]))

    result = await connect_first(
        [refused, open_port], attempt_limit=ATTEMPT_LIMIT, budget=lambda limit: (limit, False)
    )
    if result.sock is not None:
        result.sock.close()
    listener.close()

    assert result.sock is not None
    assert (result.attempts, result.kinds) == (2, ("refused",))


@pytest.mark.usefixtures("permit_loopback")
async def test_connecting_tries_at_most_three_addresses_and_reports_each_failure() -> None:
    dead = [(socket.AF_INET, (LOOPBACK, closed_port())) for _ in range(MAX_TCP_ATTEMPTS + 2)]

    result = await connect_first(
        dead, attempt_limit=ATTEMPT_LIMIT, budget=lambda limit: (limit, False)
    )

    assert result.sock is None
    assert result.kinds == ("refused",) * MAX_TCP_ATTEMPTS


@pytest.mark.usefixtures("permit_loopback")
async def test_an_exhausted_overall_budget_stops_the_attempts_before_connecting() -> None:
    address = (socket.AF_INET, (LOOPBACK, closed_port()))

    result = await connect_first(
        [address], attempt_limit=ATTEMPT_LIMIT, budget=lambda limit: (UNBOUND, True)
    )

    assert (result.sock, result.kinds) == (None, ("overall",))


class BlockingLookup:
    """Replaces socket.getaddrinfo with a lookup that holds its thread until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Semaphore(0)
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, host: str, *args: object, **kwargs: object) -> list[object]:
        with self._lock:
            self.calls.append(host)
        self.started.release()
        self.release.wait(WAIT_SECONDS)
        return []


async def started(lookup: BlockingLookup) -> None:
    assert await asyncio.to_thread(lookup.started.acquire, True, WAIT_SECONDS)


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_the_resolver_never_has_more_lookups_in_flight_than_it_has_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = BlockingLookup()
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    resolver = BoundedResolver(workers=1)
    first = asyncio.create_task(resolver("one.test", 443))
    await started(lookup)
    second = asyncio.create_task(resolver("two.test", 443))
    await settle()

    in_flight = list(lookup.calls)
    lookup.release.set()
    await asyncio.gather(first, second)
    resolver.close()

    assert in_flight == ["one.test"]
    assert lookup.calls == ["one.test", "two.test"]


async def test_a_slot_stays_taken_after_the_caller_gives_up_until_the_thread_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = BlockingLookup()
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    resolver = BoundedResolver(workers=1)
    abandoned = asyncio.create_task(resolver("one.test", 443))
    await started(lookup)
    abandoned.cancel()
    waiting = asyncio.create_task(resolver("two.test", 443))
    await settle()

    in_flight = list(lookup.calls)
    lookup.release.set()
    await waiting
    resolver.close()

    assert abandoned.cancelled()
    assert in_flight == ["one.test"]


async def test_the_resolver_resolves_a_name_to_stream_addresses() -> None:
    resolver = BoundedResolver(workers=1)

    found = await resolver("localhost", 5000)
    resolver.close()

    assert {entry[4][1] for entry in found} == {5000}
    assert {entry[1] for entry in found} == {socket.SOCK_STREAM}


async def test_connecting_skips_a_refused_address_and_reports_address_refused() -> None:
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    listener.listen(1)
    internal = (socket.AF_INET, (LOOPBACK, listener.getsockname()[1]))

    result = await connect_first(
        [internal], attempt_limit=ATTEMPT_LIMIT, budget=lambda limit: (limit, False)
    )
    listener.setblocking(False)
    try:
        accepted = listener.accept()
    except BlockingIOError:
        accepted = None
    listener.close()

    assert result.sock is None
    assert result.attempts == 0
    assert result.kinds == ("address_refused",)
    assert accepted is None


@pytest.mark.usefixtures("permit_loopback")
async def test_connecting_dials_a_permitted_address_after_skipping_a_refused_one() -> None:
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    listener.listen(1)
    metadata = (socket.AF_INET, ("169.254.169.254", 80))
    open_port = (socket.AF_INET, (LOOPBACK, listener.getsockname()[1]))

    result = await connect_first(
        [metadata, open_port], attempt_limit=ATTEMPT_LIMIT, budget=lambda limit: (limit, False)
    )
    if result.sock is not None:
        result.sock.close()
    listener.close()

    assert result.sock is not None
    assert result.kinds == ()
