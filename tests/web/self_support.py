"""Running the application as a dockmin that knows which container it runs in."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI

from app.self_identity import HomeEnvironment, SelfIdentity
from app.services.instance import RestartDispatcher
from tests.access_support import container_id
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.environment_support import World

SELF_ID = container_id(0xB1)
SELF_NAME = "dockmin-self"
SELF_VOLUME = "dockmin-state"
SETTLE_TURNS = 1000


def identity_of(
    world: World,
    *,
    restartable: bool = True,
    unreadable: Sequence[int] = (),
    volumes: Sequence[str] = (),
    source: Any = "mountinfo",
) -> SelfIdentity:
    """Build the identity of a dockmin whose container the local environment reports."""
    return SelfIdentity(
        container_id=SELF_ID,
        source=source,
        verified=(HomeEnvironment(world.local_id, "unix_socket", SELF_NAME),),
        volumes={world.local_id: frozenset(volumes), world.remote_id: frozenset(volumes)},
        restartable_via=world.local_id if restartable else None,
        unreadable=tuple(unreadable),
    )


def adopt(app: FastAPI, identity: SelfIdentity) -> SelfIdentity:
    """Make the running application hold this identity."""
    app.state.self_identity = identity
    return identity


def seed_self(host: ContainerEngine, **fields: Any) -> None:
    """Put dockmin's own container on the daemon."""
    host.add(SELF_ID, SELF_NAME, **fields)


@dataclass
class Pauses:
    """The delays the restart dispatcher was asked to wait, none of them waited."""

    asked: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)


def instant_dispatch(app: FastAPI) -> tuple[RestartDispatcher, Pauses]:
    """Replace the dispatcher with one that does not wait before it sends."""
    pauses = Pauses()
    dispatcher = RestartDispatcher(sleep=pauses)
    app.state.restart_dispatcher = dispatcher
    return dispatcher, pauses


async def settle(dispatcher: RestartDispatcher) -> None:
    """Let every dispatched restart run to its end."""
    for _ in range(SETTLE_TURNS):
        if not dispatcher.pending:
            return
        await asyncio.sleep(0)


async def events(app: FastAPI, where: str) -> list[dict[str, Any]]:
    """Return action, result and reason of the activity rows matching a condition."""
    async with app.state.database.session() as db:
        found = await db.fetch_limited(
            "SELECT action, result, target_id, json_extract(detail, '$.reason') AS reason "
            f"FROM activity_event WHERE {where} ORDER BY id",
            (),
            limit=100,
        )
    return [dict(row) for row in found]


async def refusals(app: FastAPI) -> list[dict[str, Any]]:
    """Return the rows recorded as refused to protect dockmin's own container."""
    return await events(app, "json_extract(detail, '$.reason') = 'self_protected'")
