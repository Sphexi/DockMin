"""An httpx transport whose connections pass the address policy at connect time."""

import asyncio
import ssl
from typing import Any

import httpcore
import httpx

from app import netcheck, netpolicy
from app.netpolicy import AddressRefused as AddressRefused


class _PolicyBackend(httpcore.AsyncNetworkBackend):
    """Resolves, checks and dials a checked literal; TLS still names the URL's host."""

    def __init__(self, resolver: netcheck.Resolver) -> None:
        self._resolver = resolver
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        failure: Exception | None = None
        for dial in (await self._candidates(host, port, timeout))[: netcheck.MAX_TCP_ATTEMPTS]:
            try:
                return await self._inner.connect_tcp(
                    dial, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure or httpcore.ConnectError("no address to connect to")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)

    async def _candidates(self, host: str, port: int, limit: float | None) -> list[str]:
        policy = netpolicy.current()
        literal = netpolicy.ip_literal(host)
        if literal is not None:
            if not policy.allows(literal):
                raise netpolicy.refusal(literal)
            return [host]
        try:
            async with asyncio.timeout(limit):
                infos = await self._resolver(host, port)
        except TimeoutError:
            raise httpcore.ConnectTimeout("name lookup timed out") from None
        except OSError:
            raise httpcore.ConnectError("name lookup failed") from None
        resolved = [
            (address, str(sockaddr[0]))
            for _, sockaddr in netcheck.usable_addresses(infos)
            if (address := netpolicy.sockaddr_address(sockaddr)) is not None
        ]
        if not resolved:
            raise httpcore.ConnectError("name resolved to no usable address")
        allowed = [dial for address, dial in resolved if policy.allows(address)]
        if not allowed:
            raise netpolicy.refusal(resolved[0][0])
        return allowed


class PolicyTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connections pass the address policy at connect time.

    A refusal raises AddressRefused, which is not an httpx.HTTPError.
    """

    def __init__(
        self,
        *,
        verify: ssl.SSLContext | bool,
        limits: httpx.Limits | None = None,
        resolver: netcheck.Resolver | None = None,
    ) -> None:
        limits = limits or httpx.Limits()
        context = httpx.create_ssl_context(verify=verify, trust_env=False)
        super().__init__(verify=context, trust_env=False, limits=limits)
        assert isinstance(self._pool, httpcore.AsyncConnectionPool)
        self._owned: netcheck.BoundedResolver | None = None
        if resolver is None:
            self._owned = netcheck.BoundedResolver()
            resolver = self._owned
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=_PolicyBackend(resolver),
        )

    async def aclose(self) -> None:
        """Close the connections and stop the name-lookup threads this transport owns."""
        await super().aclose()
        if self._owned is not None:
            self._owned.close()


def policy_transport(
    *,
    verify: ssl.SSLContext | bool,
    limits: httpx.Limits | None = None,
    resolver: netcheck.Resolver | None = None,
) -> PolicyTransport:
    """Build a transport that refuses internal addresses the policy does not permit."""
    return PolicyTransport(verify=verify, limits=limits, resolver=resolver)
