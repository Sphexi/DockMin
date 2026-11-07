"""Fetch of a JWKS document over https, under the address policy and bounded in size and time."""

import asyncio
import ssl
from collections.abc import Collection
from typing import Final

import httpx

from app import netpolicy
from app.auth.jwt_key_documents import KeyDocumentError, VerificationKey, parse_key_document
from app.auth.jwt_keys import KeyFetchError
from app.net_transport import policy_transport
from app.registry_client.tls import system_trust_context

FETCH_TIMEOUT: Final = 5.0
MAX_DOCUMENT_BYTES: Final = 64 * 1024
USER_AGENT: Final = "dockmin"
_CAUSE_DEPTH: Final = 6


class JwksFetcher:
    """Fetches one JWKS document over https, without redirects, within a size and time bound."""

    def __init__(
        self,
        url: str,
        allowed: Collection[str],
        *,
        trust: ssl.SSLContext | None = None,
        timeout: float = FETCH_TIMEOUT,
        max_bytes: int = MAX_DOCUMENT_BYTES,
    ) -> None:
        self._url = url
        self._allowed = tuple(allowed)
        self._trust = trust
        self._timeout = timeout
        self._max_bytes = max_bytes

    async def fetch(self) -> list[VerificationKey]:
        """Return the usable keys of the document, or raise KeyFetchError."""
        if not self._url.startswith("https://"):
            raise KeyFetchError("not_https")
        body = await self._download()
        try:
            keys = parse_key_document(body, self._allowed)
        except KeyDocumentError:
            raise KeyFetchError("invalid_document") from None
        if not keys:
            raise KeyFetchError("no_usable_keys")
        return keys

    async def _download(self) -> bytes:
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
                async with client.stream(
                    "GET",
                    self._url,
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "User-Agent": USER_AGENT,
                    },
                ) as response:
                    return await self._read(response)
        except KeyFetchError:
            raise
        except TimeoutError:
            raise KeyFetchError("timeout") from None
        except netpolicy.AddressRefused:
            raise KeyFetchError("address_refused") from None
        except ssl.SSLError:
            raise KeyFetchError("tls") from None
        except (httpx.HTTPError, OSError) as error:
            raise KeyFetchError("tls" if _is_tls_failure(error) else "network") from None
        finally:
            if client is not None:
                await client.aclose()

    async def _read(self, response: httpx.Response) -> bytes:
        if 300 <= response.status_code < 400:
            raise KeyFetchError("redirect")
        if response.status_code != httpx.codes.OK:
            raise KeyFetchError("status")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise KeyFetchError("content_encoding")
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise KeyFetchError("too_large")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self._max_bytes:
                raise KeyFetchError("too_large")
        return bytes(body)


def _is_tls_failure(error: BaseException) -> bool:
    cause: BaseException | None = error
    for _ in range(_CAUSE_DEPTH):
        if cause is None:
            return False
        if isinstance(cause, ssl.SSLError):
            return True
        cause = cause.__cause__ or cause.__context__
    return False
