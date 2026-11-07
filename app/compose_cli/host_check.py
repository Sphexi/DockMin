"""Checks a TCP Compose target's host against the address policy before a process starts."""

import asyncio
from typing import Final
from urllib.parse import urlsplit

from app import netpolicy
from app.compose_cli.errors import ComposeHostRefused
from app.compose_cli.target import ComposeTarget
from app.netcheck import Resolver, usable_addresses

RESOLVE_SECONDS: Final = 5.0
UNRESOLVED: Final = "The environment's host name could not be resolved from this server."


class ComposeHostUnresolved(ComposeHostRefused):
    """The host name has no address to check."""


async def check_target_host(target: ComposeTarget, resolver: Resolver) -> None:
    """Raise ComposeHostRefused unless every address of a TCP target's host is permitted.

    A host that does not resolve is refused.
    """
    parts = urlsplit(target.docker_host)
    if parts.scheme != "tcp" or parts.hostname is None:
        return
    try:
        port = parts.port or 0
    except ValueError:
        port = 0
    literal = netpolicy.ip_literal(parts.hostname)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = await _resolved(parts.hostname, port, resolver)
    policy = netpolicy.current()
    for address in addresses:
        if not policy.allows(address):
            netpolicy.log_refusal(str(address))
            raise ComposeHostRefused(netpolicy.REFUSAL_TEXT)


async def _resolved(host: str, port: int, resolver: Resolver) -> list[netpolicy.IpAddress]:
    try:
        async with asyncio.timeout(RESOLVE_SECONDS):
            infos = await resolver(host, port)
    except (OSError, TimeoutError):
        raise ComposeHostUnresolved(UNRESOLVED) from None
    found = [
        address
        for _, sockaddr in usable_addresses(infos)
        if (address := netpolicy.sockaddr_address(sockaddr)) is not None
    ]
    if not found:
        raise ComposeHostUnresolved(UNRESOLVED)
    return found
