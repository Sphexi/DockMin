"""Builders and readers shared by the network and volume service and page tests."""

import asyncio
import re
from html import unescape
from typing import Any

from httpx import Response

from app.db.engine import Session
from tests.access_support import container_id
from tests.docker_engine.container_engine import Box, ContainerEngine
from tests.environment_support import activity_rows

STACK_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.stack.namespace"
HTMX = {"HX-Request": "true"}
ENGINE_SECRET = "engine detail other-container-7a1b"


async def rows_of(db: Session, action: str | None = None) -> list[dict[str, Any]]:
    """Return the recorded events, oldest first, optionally of one action."""
    found = await activity_rows(db)
    return [row for row in found if action is None or row["action"] == action]


async def drain_background() -> None:
    """Wait until the tasks the services leave running after a cancellation have finished."""
    for _ in range(10):
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


def volume_mount(name: str, target: str = "/data") -> dict[str, Any]:
    """A container mount document for a named volume."""
    return {
        "Type": "volume",
        "Name": name,
        "Source": f"/var/lib/docker/volumes/{name}/_data",
        "Destination": target,
        "RW": True,
    }


def endpoint(network_id: str, address: str = "172.30.0.2") -> dict[str, Any]:
    """A container endpoint document on a network."""
    return {"NetworkID": network_id, "IPAddress": address, "Aliases": []}


def add_container(
    host: ContainerEngine,
    number: int,
    name: str,
    *,
    state: str = "exited",
    mounts: list[dict[str, Any]] | None = None,
    networks: dict[str, str] | None = None,
    **fields: Any,
) -> Box:
    """Add a container to the daemon; `networks` maps a network name to its id for an endpoint."""
    box = host.add(container_id(number), name, state=state, mounts=mounts or [], **fields)
    if networks is not None:
        box.endpoints = {n: endpoint(i) for n, i in networks.items()}
        box.host_config = {"NetworkMode": next(iter(networks), "default")}
    return box


def hidden_values(response: Response, name: str) -> list[str]:
    """Return the value of every hidden input of the page with this name."""
    pattern = rf'<input type="hidden" name="{re.escape(name)}" value="([^"]*)"'
    return [unescape(value) for value in re.findall(pattern, response.text)]


def visible_text(response: Response) -> str:
    """Return the text of the page's main region, with markup removed."""
    body = response.text
    start = body.find("<main")
    body = body[start:] if start >= 0 else body
    body = re.sub(r"<template.*?</template>", "", body, flags=re.S)
    body = re.sub(r"<(script|style).*?</\1>", "", body, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", body)).split())
