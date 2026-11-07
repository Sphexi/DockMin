"""Download of a template catalogue document over https, under the address policy and bounded."""

import asyncio
import ssl
import zlib
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from app import netpolicy
from app.auth.errors import ProductError
from app.net_transport import policy_transport
from app.registry_client.tls import system_trust_context

FETCH_TIMEOUT: Final = 20.0
MAX_BODY_BYTES: Final = 8 * 1024 * 1024
MAX_REDIRECTS: Final = 3
MAX_URL_CHARS: Final = 2048
USER_AGENT: Final = "dockmin"
_CAUSE_DEPTH: Final = 6
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_ENCODINGS: Final = frozenset({"", "identity", "gzip"})

FAILURE_TEXT: Final = {
    "not_https": (
        "The address, or a redirect it followed, is not an https address without user information."
    ),
    "too_many_redirects": "The server redirected more than 3 times.",
    "address_refused": netpolicy.REFUSAL_TEXT,
    "timeout": "The server did not answer within 20 seconds.",
    "tls": "The server's certificate could not be verified.",
    "network": "The server could not be reached.",
    "status": "The server did not answer with a document.",
    "too_large": "The document is larger than 8 MiB.",
    "unsupported_encoding": "The server answered with an encoding that is not supported.",
    "invalid_document": "The document is not a catalogue of the chosen kind.",
}
GENERIC_FAILURE_TEXT: Final = "The catalogue could not be refreshed."


class CatalogueFetchError(ProductError):
    """A refresh that failed; `code` is a product code and the message quotes no remote text."""

    def __init__(self, code: str) -> None:
        super().__init__(FAILURE_TEXT.get(code, GENERIC_FAILURE_TEXT))
        self.code = code


def failure_text(code: str | None) -> str:
    """Return the product wording of a stored failure code."""
    return FAILURE_TEXT.get(code or "", GENERIC_FAILURE_TEXT)


def url_refusal(url: str) -> str | None:
    """Return why a catalogue address is refused, or None when it is acceptable."""
    if not 9 <= len(url) <= MAX_URL_CHARS or not url.isascii() or not url.isprintable():
        return "Enter an https address of at most 2,048 printable ASCII characters."
    if url[:8].lower() != "https://" or any(c.isspace() for c in url):
        return "Enter an https address."
    if "#" in url:
        return "The address may not have a fragment."
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "Enter a valid https address."
    if not parts.hostname:
        return "Enter an https address with a host name."
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return "The address may not carry a user name or password."
    if port == 0:
        return "Enter a valid https address."
    return None


@dataclass(frozen=True, slots=True)
class FetchedBody:
    """A response body as it came over the wire, with its content encoding."""

    data: bytes
    encoding: str


class DocumentFetcher(Protocol):
    """Anything that can download a catalogue document."""

    async def fetch(self, url: str) -> FetchedBody:
        """Return the document at url, or raise CatalogueFetchError."""
        ...


class CatalogueFetcher:
    """Fetches one catalogue document over https, bounded in size and time."""

    def __init__(
        self,
        *,
        trust: ssl.SSLContext | None = None,
        timeout: float = FETCH_TIMEOUT,
        max_bytes: int = MAX_BODY_BYTES,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._trust = trust
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects

    async def fetch(self, url: str) -> FetchedBody:
        """Return the document at url, or raise CatalogueFetchError with a product code."""
        if url_refusal(url) is not None:
            raise CatalogueFetchError("not_https")
        client: httpx.AsyncClient | None = None
        try:
            async with asyncio.timeout(self._timeout):
                transport = policy_transport(
                    verify=self._trust or system_trust_context(),
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                )
                client = httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(self._timeout),
                )
                return await self._follow(client, url)
        except CatalogueFetchError:
            raise
        except TimeoutError:
            raise CatalogueFetchError("timeout") from None
        except netpolicy.AddressRefused:
            raise CatalogueFetchError("address_refused") from None
        except ssl.SSLError:
            raise CatalogueFetchError("tls") from None
        except (httpx.HTTPError, OSError) as error:
            raise CatalogueFetchError("tls" if _is_tls_failure(error) else "network") from None
        finally:
            if client is not None:
                await client.aclose()

    async def _follow(self, client: httpx.AsyncClient, url: str) -> FetchedBody:
        current = url
        for _ in range(self._max_redirects + 1):
            async with client.stream(
                "GET",
                current,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    current = _redirect_target(current, response)
                    continue
                return await self._read(response)
        raise CatalogueFetchError("too_many_redirects")

    async def _read(self, response: httpx.Response) -> FetchedBody:
        if response.status_code != httpx.codes.OK:
            raise CatalogueFetchError("status")
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in _ENCODINGS:
            raise CatalogueFetchError("unsupported_encoding")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise CatalogueFetchError("too_large")
        body = bytearray()
        async for chunk in response.aiter_raw():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                raise CatalogueFetchError("too_large")
        return FetchedBody(bytes(body), encoding)


def decode_body(body: FetchedBody, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Return the document bytes, decompressing within max_bytes; run it off the event loop."""
    if body.encoding in ("", "identity"):
        return body.data
    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        decoded = decompressor.decompress(body.data, max_bytes + 1)
    except zlib.error:
        raise CatalogueFetchError("invalid_document") from None
    if len(decoded) > max_bytes or decompressor.unconsumed_tail:
        raise CatalogueFetchError("too_large")
    if not decompressor.eof:
        raise CatalogueFetchError("invalid_document")
    return decoded


def _redirect_target(current: str, response: httpx.Response) -> str:
    location = response.headers.get("location", "")
    if not location or len(location) > MAX_URL_CHARS:
        raise CatalogueFetchError("status")
    target = str(urldefrag(urljoin(current, location)).url)
    if url_refusal(target) is not None:
        raise CatalogueFetchError("not_https")
    return target


def _is_tls_failure(error: BaseException) -> bool:
    cause: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if cause is None:
            return False
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False
