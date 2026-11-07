"""Tests for the terminal pages: the form, the ticket request and the links that lead to them."""

import re
from dataclasses import dataclass
from types import SimpleNamespace

from httpx import AsyncClient, Response

from app.services.activity import UserActor
from app.services.container_restrictions import option_label
from app.services.object_locks import LockHolder, ObjectKey
from app.web.container_terminal_routes import USER_NOT_OFFERED
from tests.auth_support import STANDARD_ID, csrf_form
from tests.container_support import API, CACHE, DB, JOB
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.terminal_engine import TerminalEngine
from tests.environment_support import activity_rows
from tests.terminal_support import (
    CONTAINER,
    PLAIN_CONTAINER,
    PRIVILEGED,
    Live,
    connect,
    read_until,
    redeem,
    rows_of,
    until,
)
from tests.web.environment_support import World

TICKET = re.compile(r'data-terminal-ticket="([A-Za-z0-9_-]{43})"')
SOCKET = re.compile(r'data-terminal-ws="([^"]+)"')
NOSCRIPT = re.compile(r"<noscript>\s*<div[^>]*>\s*<h2[^>]*>JavaScript is required</h2>")
TWO_TERMINALS = "You have 2 terminals open. Close one, then try again."
EXEC_LINK = "?mode=exec"
ATTACH_LINK = "?mode=attach"


def terminal_path(world: World, container: str, mode: str | None = None) -> str:
    query = "" if mode is None else f"?mode={mode}"
    return f"/environments/{world.remote_id}/containers/{container}/terminal{query}"


def local_path(world: World, container: str, mode: str = "exec") -> str:
    return f"/environments/{world.local_id}/containers/{container}/terminal?mode={mode}"


def detail_path(world: World, container: str) -> str:
    return f"/environments/{world.remote_id}/containers/{container}"


async def post(client: AsyncClient, path: str, **fields: str) -> tuple[int, str, dict[str, str]]:
    response = await client.post(path, data=csrf_form(client, **fields))
    return response.status_code, response.text, dict(response.headers)


def docker_mutations(host: ContainerEngine) -> list[str]:
    return [r.path for r in host.requests if "/exec" in r.path or "/attach" in r.path]


@dataclass(slots=True)
class RolePages:
    admin_form: Response
    standard_form: Response
    writer_detail: Response
    read_only_detail: Response
    admin_stopped_detail: Response
    restricted_form: Response
    restricted_detail: Response


async def role_pages(admin: AsyncClient, standard: AsyncClient, world: World) -> RolePages:
    return RolePages(
        admin_form=await admin.get(terminal_path(world, DB)),
        standard_form=await standard.get(terminal_path(world, DB)),
        writer_detail=await standard.get(detail_path(world, DB)),
        read_only_detail=await standard.get(detail_path(world, JOB)),
        admin_stopped_detail=await admin.get(detail_path(world, JOB)),
        restricted_form=await standard.get(terminal_path(world, PRIVILEGED)),
        restricted_detail=await standard.get(detail_path(world, PRIVILEGED)),
    )


async def test_the_pages_offer_each_role_only_what_it_may_use_and_say_why_when_it_may_not(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    restricted_stack: list[str],
    shared_host: ContainerEngine,
) -> None:
    pages = await role_pages(admin_client, standard_client, world)

    assert (pages.admin_form.status_code, pages.standard_form.status_code) == (200, 200)
    assert pages.admin_form.headers["cache-control"] == "no-store"
    assert 'name="user"' in pages.admin_form.text and 'name="user"' not in pages.standard_form.text
    assert 'name="command"' in pages.standard_form.text
    assert 'name="csrf_token"' in pages.standard_form.text
    assert "Attach is not available:" in pages.standard_form.text
    assert EXEC_LINK in pages.writer_detail.text and ATTACH_LINK not in pages.writer_detail.text
    assert EXEC_LINK not in pages.read_only_detail.text
    assert EXEC_LINK not in pages.admin_stopped_detail.text
    reason = (
        "Only administrators may open a terminal in a container that uses "
        f"{option_label('privileged')}."
    )
    assert reason in pages.restricted_form.text and reason in pages.restricted_detail.text
    assert 'name="command"' not in pages.restricted_form.text
    assert EXEC_LINK not in pages.restricted_detail.text
    assert docker_mutations(shared_host) == []


async def test_the_ticket_request_needs_the_token_and_issues_a_ticket_without_opening_anything(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    refused = await admin_client.post(terminal_path(world, DB), data={"command": "/bin/sh"})
    status, page, headers = await post(
        admin_client, terminal_path(world, DB), command="/bin/sh", rows="30", cols="100"
    )

    assert refused.status_code == 403
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert TICKET.search(page) is not None and SOCKET.search(page) is not None
    assert NOSCRIPT.search(page) is not None
    assert "A terminal needs JavaScript." in page
    assert "Press Ctrl+Shift+F6 to move focus out of the terminal." in page
    assert docker_mutations(shared_host) == []
    async with world.app.state.database.session() as db:
        assert await activity_rows(db) == []


async def refusals(
    standard: AsyncClient, admin: AsyncClient, world: World
) -> dict[str, tuple[int, str]]:
    cases: dict[str, tuple[AsyncClient, str, dict[str, str]]] = {
        "restricted": (standard, terminal_path(world, PRIVILEGED), {}),
        "read-only": (standard, terminal_path(world, JOB), {}),
        "hidden": (standard, terminal_path(world, CACHE), {}),
        "stopped": (admin, terminal_path(world, JOB), {}),
        "busy": (admin, terminal_path(world, API), {}),
        "attach on a plain container": (admin, terminal_path(world, DB, "attach"), {}),
        "unknown mode": (admin, terminal_path(world, DB, "root"), {}),
        "bad command": (admin, terminal_path(world, DB), {"command": "ls 'oops"}),
        "standard chooses the user": (standard, terminal_path(world, DB), {"user": "root"}),
        "no terminal left": (standard, terminal_path(world, DB), {}),
    }
    results: dict[str, tuple[int, str]] = {}
    for label, (client, path, fields) in cases.items():
        status, page, _ = await post(client, path, **fields)
        results[label] = (status, page)
    return results


async def test_a_ticket_request_that_cannot_be_granted_is_refused_with_a_page_that_says_why(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    restricted_stack: list[str],
    shared_host: ContainerEngine,
) -> None:
    engine = world.app.state.engine
    busy = ObjectKey(world.remote_id, "container", API)
    taken = [engine.stream_slots.acquire(STANDARD_ID, kind="terminal") for _ in range(2)]
    async with engine.locks.hold(busy, LockHolder(UserActor(1), "someone-else", "stop")):
        outcomes = await refusals(standard_client, admin_client, world)

    statuses = {label: status for label, (status, _) in outcomes.items()}
    pages = {label: page for label, (_, page) in outcomes.items()}
    assert statuses == {
        "restricted": 403,
        "read-only": 403,
        "hidden": 404,
        "stopped": 409,
        "busy": 409,
        "attach on a plain container": 409,
        "unknown mode": 404,
        "bad command": 400,
        "standard chooses the user": 400,
        "no terminal left": 429,
    }
    assert option_label("privileged") in pages["restricted"]
    assert "The container is not running" in pages["stopped"]
    assert "someone-else" not in pages["busy"]
    assert "This container cannot be attached to" in pages["attach on a plain container"]
    assert "unclosed quotation mark" in pages["bad command"]
    assert "ls &#39;oops" in pages["bad command"]
    forged = pages["standard chooses the user"]
    assert "Check the form" in forged and USER_NOT_OFFERED in forged
    assert 'name="command"' in forged and 'name="user"' not in forged
    assert TWO_TERMINALS in pages["no terminal left"]
    assert docker_mutations(shared_host) == []
    assert len(taken) == engine.stream_slots.open_count


async def test_the_attach_pages_warn_or_point_at_exec_and_each_mode_gets_its_own_ticket_page(
    admin_client: AsyncClient, world: World, terminal_daemon: TerminalEngine
) -> None:
    interactive = await admin_client.get(local_path(world, CONTAINER, "attach"))
    plain = await admin_client.get(local_path(world, PLAIN_CONTAINER, "attach"))
    exec_ticket = await post(admin_client, local_path(world, CONTAINER, "exec"))
    attach_ticket = await post(admin_client, local_path(world, CONTAINER, "attach"))

    assert "Input goes to the container's main process" in interactive.text
    assert "Ctrl-C or Ctrl-D can stop the container" in interactive.text
    assert "Connect" in interactive.text
    assert "A terminal cannot be opened" in plain.text
    assert "Open a terminal with exec instead" in plain.text
    assert 'name="command"' not in plain.text
    assert (exec_ticket[0], attach_ticket[0]) == (200, 200)
    assert 'data-terminal-mode="exec"' in exec_ticket[1]
    assert 'data-terminal-mode="attach"' in attach_ticket[1]
    assert f"/containers/{CONTAINER}/terminal/ws" in exec_ticket[1] + attach_ticket[1]


async def test_the_ticket_on_the_page_opens_a_session_over_the_socket_with_its_rows_written(
    admin_client: AsyncClient, live: Live, world: World, terminal_daemon: TerminalEngine
) -> None:
    status, page, _ = await post(
        admin_client, local_path(world, CONTAINER), command="/bin/sh -c ok", rows="30", cols="100"
    )
    ticket = TICKET.search(page)
    path = SOCKET.search(page)
    assert ticket is not None and path is not None

    async with connect(live, path.group(1)) as socket:
        await redeem(socket, SimpleNamespace(ticket=ticket.group(1)))  # type: ignore[arg-type]
        await socket.send(b"ping\r")
        echoed = await read_until(socket, b"ping\r")

    await until(lambda: len(terminal_daemon.sessions) == 1)
    started = await rows_of(world.app, "container_exec_started")
    assert (status, echoed) == (200, b"ping\r")
    assert started[0]["detail"]["command"] == "/bin/sh"
    assert terminal_daemon.execs[next(iter(terminal_daemon.execs))].body["ConsoleSize"] == [30, 100]
