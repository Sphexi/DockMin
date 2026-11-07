"""The Swarm gate: only a visible manager reaches the Engine, and a stale type is repaired."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.errors import EngineError
from app.services.container_logs import LogQuery
from app.services.object_inspector import EnvironmentUnavailable, SwarmUnavailable
from app.services.swarm_gate import require_swarm, swarm_unavailable_from
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import STANDARD_ID
from tests.automation_support import authority_context
from tests.docker_engine.swarm_engine import MANAGER, WORKER
from tests.services.conftest import SwarmRig
from tests.swarm_support import MISSING_ID
from tests.updater_support import UPDATER

FORM = {"name": "web", "image": "busybox:latest"}
PLAIN_NAME_FORM = {"name": "c", "content": "x"}
SECRET_FORM = {"name": "s", "value": "v"}

Entry = Callable[[SwarmRig, Principal, int], Awaitable[Any]]

ENTRY_POINTS: list[tuple[str, Entry]] = [
    ("overview", lambda r, who, env: r.sw.queries.overview(who, env)),
    ("nodes", lambda r, who, env: r.sw.queries.nodes(who, env)),
    ("node", lambda r, who, env: r.sw.queries.node(who, env, WORKER)),
    ("task", lambda r, who, env: r.sw.queries.task(who, env, MISSING_ID)),
    (
        "availability impact",
        lambda r, who, env: r.sw.queries.availability_impact(who, env, WORKER, "drain"),
    ),
    (
        "set availability",
        lambda r, who, env: r.sw.nodes.set_availability(
            who,
            env,
            WORKER,
            availability="drain",
            expected_version=1,
            expected_availability="active",
        ),
    ),
    ("service list", lambda r, who, env: r.sw.services.page(who, env)),
    ("service detail", lambda r, who, env: r.sw.services.detail(who, env, MISSING_ID)),
    ("service tasks", lambda r, who, env: r.sw.services.tasks(who, env, MISSING_ID)),
    ("create offer", lambda r, who, env: r.sw.services.create_offer(who, env)),
    ("edit offer", lambda r, who, env: r.sw.services.edit_offer(who, env, MISSING_ID)),
    ("create service", lambda r, who, env: r.sw.service_actions.create(who, env, FORM)),
    (
        "scale",
        lambda r, who, env: r.sw.service_actions.scale(
            who, env, MISSING_ID, replicas=2, expected_version=1, expected_replicas=1
        ),
    ),
    (
        "remove service",
        lambda r, who, env: r.sw.service_actions.remove(
            who, env, MISSING_ID, expected_name="x", expected_version=1
        ),
    ),
    (
        "service logs",
        lambda r, who, env: r.sw.logs.open(who, env, MISSING_ID, LogQuery()),
    ),
    (
        "service log tail",
        lambda r, who, env: r.sw.logs.tail(who, env, MISSING_ID, LogQuery()),
    ),
    ("config list", lambda r, who, env: r.sw.configs.page(who, env)),
    ("config detail", lambda r, who, env: r.sw.configs.detail(who, env, MISSING_ID)),
    ("create config", lambda r, who, env: r.sw.config_actions.create(who, env, PLAIN_NAME_FORM)),
    ("secret list", lambda r, who, env: r.sw.secrets.page(who, env)),
    ("secret detail", lambda r, who, env: r.sw.secrets.detail(who, env, MISSING_ID)),
    ("create secret", lambda r, who, env: r.sw.secret_actions.create(who, env, SECRET_FORM)),
    ("stack list", lambda r, who, env: r.sw.stacks.page(who, env)),
    ("stack detail", lambda r, who, env: r.sw.stacks.detail(who, env, "shop")),
]

NOT_MANAGERS = [
    pytest.param("standalone", "inactive", 0, id="standalone"),
    pytest.param("standalone", "active", 0, id="worker"),
    pytest.param(None, "inactive", 0, id="never contacted"),
]


@pytest.mark.parametrize("who", [ADMIN, STANDARD], ids=["administrator", "standard"])
@pytest.mark.parametrize(("kind", "state", "control"), NOT_MANAGERS)
async def test_every_entry_point_is_not_found_before_any_engine_call_off_a_manager(
    swarm: SwarmRig, who: Principal, kind: str | None, state: str, control: int
) -> None:
    await swarm.db.execute(
        "UPDATE environment SET type = ?, swarm_state = ?, "
        "swarm_control_available = ? WHERE id = ?",
        (kind, state, control, ENV_A),
    )
    swarm.fresh()
    swarm.host.requests.clear()

    outcomes = []
    for _, entry in ENTRY_POINTS:
        with pytest.raises(ResourceNotFound) as raised:
            await entry(swarm, who, ENV_A)
        outcomes.append(str(raised.value))

    assert swarm.host.requests == []
    assert len(set(outcomes)) == 1


@pytest.mark.parametrize("name", [name for name, _ in ENTRY_POINTS])
async def test_a_missing_environment_and_a_hidden_one_read_as_the_same_not_found(
    swarm: SwarmRig, name: str
) -> None:
    entry = dict(ENTRY_POINTS)[name]
    await EnvironmentAccessRepository(swarm.db).remove_user("environment", ENV_A, STANDARD_ID)
    swarm.fresh()

    with pytest.raises(ResourceNotFound) as hidden:
        await entry(swarm, STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound) as missing:
        await entry(swarm, STANDARD, 999)

    assert str(hidden.value) == str(missing.value)
    assert [r for r in swarm.host.requests if r.path.startswith("/services")] == []


async def test_a_manager_reaches_the_engine_for_the_administrator(swarm: SwarmRig) -> None:
    overview = await swarm.sw.queries.overview(ADMIN, ENV_A)

    assert overview.nodes == 2 and swarm.host.calls("GET", "/nodes")


@pytest.mark.parametrize(
    ("status", "expected", "pokes"),
    [
        (503, SwarmUnavailable, [ENV_A]),
        (500, EnvironmentUnavailable, []),
        (400, EnvironmentUnavailable, []),
    ],
)
def test_a_503_from_a_swarm_call_asks_for_a_fresh_snapshot_and_maps_to_a_fixed_message(
    status: int, expected: type[EnvironmentUnavailable], pokes: list[int]
) -> None:
    poked: list[int] = []
    error = EngineError(ENV_A, "engine text naming service web", status_code=status)

    mapped = swarm_unavailable_from(error, ENV_A, poked.append)

    assert type(mapped) is expected and poked == pokes
    assert "web" not in str(mapped)
    assert mapped.kind == ("not_swarm_manager" if status == 503 else "engine")


async def test_the_engine_answering_503_surfaces_as_not_a_manager_and_pokes_the_refresh(
    swarm: SwarmRig,
) -> None:
    swarm.host.outage = {"services"}

    with pytest.raises(SwarmUnavailable) as raised:
        await swarm.sw.services.page(ADMIN, ENV_A)

    assert raised.value.kind == "not_swarm_manager"
    assert swarm.poker.poked == [ENV_A]


async def test_the_inspector_maps_a_503_on_a_swarm_object_read_the_same_way(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(
        {"Name": "web", "TaskTemplate": {"ContainerSpec": {"Image": "x"}}}
    )
    swarm.host.outage = {"services"}

    with pytest.raises(SwarmUnavailable):
        await swarm.inspector.service(ENV_A, service_id)

    assert swarm.poker.poked == [ENV_A]
    assert MANAGER in swarm.host.nodes


class SpyAuthoriser:
    """Records the environment gate a caller asks for, and answers as the real one does."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.gated: list[int] = []

    async def require_environment(self, principal: Principal, environment_id: int) -> None:
        self.gated.append(environment_id)
        await self._real.require_environment(principal, environment_id)


async def test_an_automation_run_is_not_asked_for_the_environment_gate_but_a_user_is(
    swarm: SwarmRig,
) -> None:
    context = await authority_context(
        swarm.db, swarm.clock, swarm.engine.locks, environment_id=ENV_A, subsystem=UPDATER
    )
    spy = SpyAuthoriser(swarm.access.authoriser)
    hosts = swarm.inspector.hosts

    run = await require_swarm(spy, hosts, context.authority(), ENV_A)  # type: ignore[arg-type]
    gated_for_run = list(spy.gated)
    user = await require_swarm(spy, hosts, STANDARD, ENV_A)  # type: ignore[arg-type]

    assert (run.type, user.type) == ("swarm", "swarm")
    assert gated_for_run == [] and spy.gated == [ENV_A]


async def test_an_automation_run_is_still_refused_an_environment_that_is_not_a_manager(
    swarm: SwarmRig,
) -> None:
    await swarm.db.execute("UPDATE environment SET type = 'standalone' WHERE id = ?", (ENV_A,))
    swarm.fresh()
    context = await authority_context(
        swarm.db, swarm.clock, swarm.engine.locks, environment_id=ENV_A, subsystem=UPDATER
    )

    with pytest.raises(ResourceNotFound):
        await require_swarm(
            swarm.access.authoriser,
            swarm.inspector.hosts,
            context.authority(),
            ENV_A,
        )

    assert swarm.host.requests == []
