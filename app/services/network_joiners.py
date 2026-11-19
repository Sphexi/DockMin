"""Containers that share another container's network namespace, and whether that share is stale."""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from app.docker_engine.client import EngineClient
from app.docker_engine.errors import NotFound
from app.docker_engine.models import ContainerInspect, ContainerSummary
from app.services.container_names import display_name, parse_engine_time

CONTAINER_MODE: Final = "container:"
MAX_STALE_CHECKS: Final = 100
MAX_PARALLEL_INSPECTS: Final = 8
MIN_ID_PREFIX: Final = 12
MAX_JOINER_CHAIN: Final = 50
MAX_JOINER_DEPTH: Final = 8
LIVE_OWNER_STATES: Final = frozenset({"running", "paused"})

type StaleReason = Literal["owner_missing", "owner_not_running", "owner_restarted"]


@dataclass(frozen=True, slots=True)
class StaleNetwork:
    """A joiner whose network namespace no longer belongs to a running owner.

    owner_id is None when the owner no longer exists, and owner_name is then the reference the
    joiner's network mode holds.
    """

    owner_id: str | None
    owner_name: str
    reason: StaleReason


def joined_reference(network_mode: str) -> str | None:
    """Return the container, by id or name, that a network mode joins; None for any other mode."""
    if not network_mode.startswith(CONTAINER_MODE):
        return None
    return network_mode.removeprefix(CONTAINER_MODE) or None


def _joins(reference: str, owner_id: str, owner_name: str, ids: Collection[str]) -> bool:
    if reference in (owner_id, owner_name):
        return True
    if len(reference) < MIN_ID_PREFIX or not owner_id.startswith(reference):
        return False
    return not any(other != owner_id and other.startswith(reference) for other in ids)


def joins_by_id(network_mode: str, owner_id: str) -> bool:
    """Report whether a network mode names the owner by its id, in full or as a prefix."""
    reference = joined_reference(network_mode)
    if reference is None:
        return False
    return reference == owner_id or (
        len(reference) >= MIN_ID_PREFIX and owner_id.startswith(reference)
    )


def joiners_of(
    rows: Iterable[ContainerSummary], owner_id: str, owner_name: str
) -> list[ContainerSummary]:
    """Return the listed containers whose network mode joins the owner.

    The mode names the owner by its id, its name, or an id prefix of at least 12 characters that
    no other listed container shares.
    """
    listed = list(rows)
    ids = [row.id for row in listed]
    return [
        row
        for row in listed
        if row.id != owner_id
        and (reference := joined_reference(row.network_mode)) is not None
        and _joins(reference, owner_id, owner_name, ids)
    ]


def joiner_owners(rows: Sequence[ContainerSummary]) -> dict[str, str]:
    """Map the id of each listed joiner to the id of its owner, for owners that are listed."""
    by_reference: dict[str, str] = {}
    for row in rows:
        by_reference[row.id] = row.id
        for name in row.names:
            plain = name.removeprefix("/")
            if plain and "/" not in plain:
                by_reference.setdefault(plain, row.id)

    def owner_of(reference: str) -> str | None:
        found = by_reference.get(reference)
        if found is not None or len(reference) < MIN_ID_PREFIX:
            return found
        matches = {row.id for row in rows if row.id.startswith(reference)}
        return matches.pop() if len(matches) == 1 else None

    owners: dict[str, str] = {}
    for row in rows:
        reference = joined_reference(row.network_mode)
        owner = None if reference is None else owner_of(reference)
        if owner is not None and owner != row.id:
            owners[row.id] = owner
    return owners


@dataclass(frozen=True, slots=True)
class JoinerLink:
    """A joiner and the id of the container whose network it joins."""

    row: ContainerSummary
    owner_id: str


@dataclass(frozen=True, slots=True)
class JoinerWalk:
    """The joiners found, owners before the containers that join them, and the count left out."""

    links: tuple[JoinerLink, ...]
    omitted: int = 0


async def walk_joiners(
    rows: Sequence[ContainerSummary],
    roots: Sequence[tuple[str, str]],
    skip: Collection[str],
    descend: Callable[[JoinerLink], Awaitable[bool]],
    *,
    limit: int = MAX_JOINER_CHAIN,
    depth: int = MAX_JOINER_DEPTH,
) -> JoinerWalk:
    """Collect the joiners of the roots, and of each joiner `descend` accepts, level by level.

    `roots` are (id, name) pairs and `skip` ids that are never collected. A joiner past `limit`
    collected or `depth` levels is counted in `omitted` instead; the count covers only joiners
    known from the listing, not those a deeper walk would have found.
    """
    seen = {*skip, *(owner_id for owner_id, _ in roots)}
    links: list[JoinerLink] = []
    omitted = 0
    frontier = list(roots)
    level = 0
    while frontier:
        level += 1
        following: list[tuple[str, str]] = []
        for owner_id, owner_name in frontier:
            for row in joiners_of(rows, owner_id, owner_name):
                if row.id in seen:
                    continue
                seen.add(row.id)
                if level > depth or len(links) >= limit:
                    omitted += 1
                    continue
                link = JoinerLink(row, owner_id)
                links.append(link)
                if await descend(link):
                    following.append((row.id, display_name(row.names, row.id)))
        frontier = following
    return JoinerWalk(tuple(links), omitted)


async def list_joiners(
    client: EngineClient, owner_id: str, owner_name: str, *, all: bool = False
) -> list[ContainerSummary]:
    """List the containers on the host that join the owner; stopped ones only when `all` is set."""
    return joiners_of(await client.containers.list(all=all), owner_id, owner_name)


def stale_reason(
    owner_status: str | None, owner_started: datetime | None, joiner_started: datetime | None
) -> StaleReason | None:
    """Say why a joiner's network is stale, or None when its owner is live and started first.

    A paused owner is live. owner_status is None for an owner that does not exist. A start time
    that is not known never makes a network stale.
    """
    if owner_status is None:
        return "owner_missing"
    if owner_status not in LIVE_OWNER_STATES:
        return "owner_not_running"
    if joiner_started is not None and owner_started is not None and owner_started > joiner_started:
        return "owner_restarted"
    return None


async def inspect_owner(client: EngineClient, reference: str) -> ContainerInspect | None:
    """Read the container a network mode joins; None when the host has no such container."""
    try:
        return await client.containers.inspect(reference)
    except NotFound:
        return None


def verdict(
    reference: str, owner: ContainerInspect | None, joiner_started: datetime | None
) -> StaleNetwork | None:
    """Judge a joiner of `reference` against its owner as the host reports it."""
    reason = stale_reason(
        None if owner is None else owner.state.status,
        None if owner is None else parse_engine_time(owner.state.started_at),
        joiner_started,
    )
    if reason is None:
        return None
    if owner is None:
        return StaleNetwork(None, reference, reason)
    return StaleNetwork(owner.id, display_name([owner.name], owner.id), reason)


async def stale_network_of(
    client: EngineClient, network_mode: str, joiner_started: datetime | None
) -> StaleNetwork | None:
    """Judge one running container's network from its mode and start time."""
    reference = joined_reference(network_mode)
    if reference is None:
        return None
    return verdict(reference, await inspect_owner(client, reference), joiner_started)


async def stale_networks(
    client: EngineClient, rows: Iterable[ContainerSummary]
) -> dict[str, StaleNetwork]:
    """Return the running joiners among the rows whose network is stale, keyed by container id.

    Only rows whose network mode joins a container are read, and each owner once, so the cost
    follows the number of joiners.
    """
    candidates = [
        row
        for row in rows
        if row.state == "running" and joined_reference(row.network_mode) is not None
    ][:MAX_STALE_CHECKS]
    gate = asyncio.Semaphore(MAX_PARALLEL_INSPECTS)

    async def owner(reference: str) -> ContainerInspect | None:
        async with gate:
            return await inspect_owner(client, reference)

    async def joiner(container_id: str) -> ContainerInspect | None:
        async with gate:
            try:
                return await client.containers.inspect(container_id)
            except NotFound:
                return None

    references = sorted({ref for row in candidates if (ref := joined_reference(row.network_mode))})
    owners = dict(
        zip(references, await asyncio.gather(*(owner(ref) for ref in references)), strict=True)
    )
    inspected = await asyncio.gather(*(joiner(row.id) for row in candidates))
    stale: dict[str, StaleNetwork] = {}
    for row, own in zip(candidates, inspected, strict=True):
        reference = joined_reference(row.network_mode)
        if own is None or reference is None:
            continue
        found = verdict(reference, owners[reference], parse_engine_time(own.state.started_at))
        if found is not None:
            stale[row.id] = found
    return stale
