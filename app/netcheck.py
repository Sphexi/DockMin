"""DNS, TCP and TLS primitives shared by the staged connection tests."""

import asyncio
import errno
import ipaddress
import socket
import ssl
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app import netpolicy
from app.netpolicy import ip_literal as ip_literal

MAX_TCP_ATTEMPTS = 3
DNS_WORKERS = 4
MAX_RESOLVED_ADDRESSES = 64
MAX_DISPLAY_CHARS = 64

HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

type AddrInfo = tuple[Any, Any, int, str, tuple[Any, ...]]
type Resolver = Callable[[str, int], Awaitable[Sequence[AddrInfo]]]
type Address = tuple[socket.AddressFamily, tuple[Any, ...]]
type FactValue = str | int
type Budget = Callable[[float], tuple[float, bool]]


class StageOutcome(StrEnum):
    """What happened at one stage."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"


class TlsFailure(StrEnum):
    """What a failed handshake says about the peer."""

    CLOSED = "closed"
    NOT_TLS = "not_tls"
    PROTOCOL_VERSION = "protocol_version"
    CERTIFICATE_ALERT = "certificate_alert"
    HANDSHAKE_REJECTED = "handshake_rejected"
    ALERT = "alert"
    OTHER = "other"


class CertificateFault(StrEnum):
    """Why the peer's certificate was refused."""

    UNKNOWN_ISSUER = "unknown_issuer"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    NAME_MISMATCH = "name_mismatch"
    WRONG_PURPOSE = "wrong_purpose"
    WEAK_KEY = "weak_key"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TcpResult:
    """The outcome of trying addresses in turn; `sock` is None when every attempt failed."""

    sock: socket.socket | None
    sockaddr: tuple[Any, ...]
    attempts: int
    kinds: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PeerFacts:
    """Non-secret facts about an established TLS session."""

    version: str
    expires: str


def seconds_text(value: float) -> str:
    """Format a duration for a message."""
    return f"{value:g} second{'' if value == 1 else 's'}"


def clean_text(value: str, limit: int = MAX_DISPLAY_CHARS) -> str:
    """Cut text supplied by a peer to `limit` characters, minus control and format marks."""
    return "".join(c for c in value[:limit] if unicodedata.category(c) not in HIDDEN_CATEGORIES)


async def system_resolver(host: str, port: int) -> Sequence[AddrInfo]:
    """Resolve on the event loop's default executor."""
    return await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )


class BoundedResolver:
    """Resolves names on a few dedicated threads, so a stalled lookup starves nothing else.

    A slot stays taken until its thread returns, even when the caller gave up, so at most
    `workers` lookups are ever in flight; the rest wait for a slot or time out waiting.
    """

    def __init__(self, workers: int = DNS_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dockmin-dns")
        self._slots = asyncio.Semaphore(workers)

    async def __call__(self, host: str, port: int) -> Sequence[AddrInfo]:
        """Resolve host to stream addresses."""
        await self._slots.acquire()
        try:
            lookup = asyncio.get_running_loop().run_in_executor(
                self._executor,
                lambda: socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
                ),
            )
        except BaseException:
            self._slots.release()
            raise
        lookup.add_done_callback(self._finished)
        return await asyncio.shield(lookup)

    def close(self) -> None:
        """Drop queued lookups and stop the threads once the running ones return."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _finished(self, lookup: "asyncio.Future[Any]") -> None:
        self._slots.release()
        if not lookup.cancelled():
            lookup.exception()


def literal_address(literal: ipaddress.IPv4Address | ipaddress.IPv6Address, port: int) -> Address:
    """Build the connectable form of an IP literal."""
    if literal.version == 6:
        return (socket.AF_INET6, (str(literal), port, 0, 0))
    return (socket.AF_INET, (str(literal), port))


def usable_addresses(infos: Sequence[AddrInfo]) -> list[Address]:
    """Keep the distinct IPv4 and IPv6 stream addresses of a lookup, up to the cap."""
    addresses: list[Address] = []
    for family, _, _, _, sockaddr in infos[:MAX_RESOLVED_ADDRESSES]:
        entry = (family, tuple(sockaddr))
        if family in (socket.AF_INET, socket.AF_INET6) and entry not in addresses:
            addresses.append(entry)
    return addresses


def dns_failure_message(code: int | None) -> str:
    """Explain a resolver error code."""
    if code == socket.EAI_NONAME or code == getattr(socket, "EAI_NODATA", None):
        return "The name does not exist. Check the spelling, or use the host's IP address."
    if code == socket.EAI_AGAIN:
        return "The DNS server did not answer. Check this server's DNS configuration."
    if code == socket.EAI_FAIL:
        return "The DNS server reported a permanent failure for this name."
    return "The name lookup failed."


async def connect_first(
    addresses: Sequence[Address],
    *,
    attempt_limit: float,
    budget: Budget,
    observe: Callable[[OSError], None] | None = None,
) -> TcpResult:
    """Try up to MAX_TCP_ATTEMPTS permitted addresses in order; return the first connected socket.

    Addresses the address policy refuses are skipped; when only those were offered the result
    carries the kind `address_refused`.
    """
    loop = asyncio.get_running_loop()
    policy = netpolicy.current()
    permitted = [entry for entry in addresses if _allowed(policy, entry[1])]
    refused = [entry for entry in addresses if entry not in permitted]
    for _, sockaddr in refused[: netpolicy.MAX_LOGGED_REFUSALS]:
        netpolicy.log_refusal(str(sockaddr[0]))
    kinds: list[str] = []
    for family, sockaddr in permitted[:MAX_TCP_ATTEMPTS]:
        seconds, overall = budget(attempt_limit)
        if seconds <= 0:
            kinds.append("overall")
            break
        candidate: socket.socket | None = None
        try:
            candidate = socket.socket(family, socket.SOCK_STREAM)
            candidate.setblocking(False)
            async with asyncio.timeout(seconds):
                await loop.sock_connect(candidate, sockaddr)
        except TimeoutError:
            kinds.append("overall" if overall else "timeout")
        except OSError as exc:
            kinds.append(tcp_failure_kind(exc.errno))
            if observe is not None:
                observe(exc)
        except BaseException:
            if candidate is not None:
                candidate.close()
            raise
        else:
            return TcpResult(candidate, sockaddr, len(kinds) + 1, tuple(kinds))
        if candidate is not None:
            candidate.close()
    return TcpResult(None, (), len(kinds), (*kinds, "address_refused") if refused else tuple(kinds))


def _allowed(policy: "netpolicy.AddressPolicy", sockaddr: tuple[Any, ...]) -> bool:
    address = netpolicy.sockaddr_address(sockaddr)
    return address is not None and policy.allows(address)


def tcp_failure_kind(code: int | None) -> str:
    """Classify a connect error number."""
    if code == errno.ECONNREFUSED:
        return "refused"
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN):
        return "unreachable"
    if code in (errno.EACCES, errno.EPERM):
        return "blocked"
    if code == errno.ETIMEDOUT:
        return "timeout"
    return "other"


_TCP_PRIORITY = (
    "refused",
    "unreachable",
    "blocked",
    "timeout",
    "overall",
    "address_refused",
    "other",
)


def tcp_failure_message(kinds: Sequence[str], *, tcp_timeout: float, total_timeout: float) -> str:
    """Explain the most telling failure among the attempts."""
    kind = next((k for k in _TCP_PRIORITY if k in kinds), "other")
    match kind:
        case "refused":
            return (
                "The connection was refused. Nothing is listening on this port, or a firewall "
                "is rejecting it."
            )
        case "unreachable":
            return "There is no route to the host. Check the address and the network path."
        case "blocked":
            return "This server's network policy blocked the connection."
        case "timeout":
            return (
                f"The connection timed out after {seconds_text(tcp_timeout)}. The host is down or "
                "a firewall is dropping the packets."
            )
        case "overall":
            return f"The test reached its overall time limit of {seconds_text(total_timeout)}."
        case "address_refused":
            return netpolicy.REFUSAL_TEXT
        case _:
            return "The connection could not be established."


_NOT_TLS_REASONS = frozenset(
    {
        "WRONG_VERSION_NUMBER",
        "HTTP_REQUEST",
        "HTTPS_PROXY_REQUEST",
        "UNKNOWN_PROTOCOL",
        "PACKET_LENGTH_TOO_LONG",
        "RECORD_LENGTH_MISMATCH",
        "WRONG_SSL_VERSION",
        "RECORD_LAYER_FAILURE",
        "BAD_PACKET_LENGTH",
    }
)
_CERTIFICATE_ALERT_MARKERS = ("CERTIFICATE", "UNKNOWN_CA", "ACCESS_DENIED", "DECRYPT_ERROR")
_OLD_PROTOCOL_REASONS = frozenset(
    {"UNSUPPORTED_PROTOCOL", "NO_PROTOCOLS_AVAILABLE", "UNSUPPORTED_SSL_VERSION"}
)


def classify_tls_failure(exc: ssl.SSLError) -> TlsFailure:
    """Classify a handshake failure from the library's reason string."""
    reason = (exc.reason or "").upper()
    if isinstance(exc, ssl.SSLEOFError) or "EOF" in reason:
        return TlsFailure.CLOSED
    if reason in _NOT_TLS_REASONS:
        return TlsFailure.NOT_TLS
    if "PROTOCOL_VERSION" in reason or reason in _OLD_PROTOCOL_REASONS:
        return TlsFailure.PROTOCOL_VERSION
    if "ALERT" in reason and any(marker in reason for marker in _CERTIFICATE_ALERT_MARKERS):
        return TlsFailure.CERTIFICATE_ALERT
    if "HANDSHAKE_FAILURE" in reason or "NO_SHARED_CIPHER" in reason:
        return TlsFailure.HANDSHAKE_REJECTED
    if "ALERT" in reason:
        return TlsFailure.ALERT
    return TlsFailure.OTHER


_UNKNOWN_ISSUER_CODES = frozenset({2, 18, 19, 20, 21})
_EXPIRED_CODE = 10
_NOT_YET_VALID_CODE = 9
_NAME_MISMATCH_CODES = frozenset({62, 64})
_WRONG_PURPOSE_CODES = frozenset({26})
_WEAK_CODES = frozenset({65, 66, 67})


def classify_certificate_fault(code: int) -> CertificateFault:
    """Classify an OpenSSL certificate verification code."""
    if code in _UNKNOWN_ISSUER_CODES:
        return CertificateFault.UNKNOWN_ISSUER
    if code == _EXPIRED_CODE:
        return CertificateFault.EXPIRED
    if code == _NOT_YET_VALID_CODE:
        return CertificateFault.NOT_YET_VALID
    if code in _NAME_MISMATCH_CODES:
        return CertificateFault.NAME_MISMATCH
    if code in _WRONG_PURPOSE_CODES:
        return CertificateFault.WRONG_PURPOSE
    if code in _WEAK_CODES:
        return CertificateFault.WEAK_KEY
    return CertificateFault.OTHER


def peer_facts(secure: ssl.SSLObject | ssl.SSLSocket | None) -> PeerFacts:
    """Read the protocol version and the certificate's expiry date from a TLS session."""
    if secure is None:
        return PeerFacts("", "")
    expires = ""
    certificate: Mapping[str, Any] | None = secure.getpeercert()
    if certificate and "notAfter" in certificate:
        try:
            moment = datetime.fromtimestamp(ssl.cert_time_to_seconds(certificate["notAfter"]), UTC)
            expires = moment.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            expires = ""
    return PeerFacts(clean_text(secure.version() or ""), expires)
