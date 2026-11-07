"""EngineSelfProbe: what an environment's Engine says about a candidate container id."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.docker_engine.errors import EngineError
from app.self_identity import ContainerFacts, EnvironmentRef
from app.services.self_identity_probe import EngineSelfProbe
from tests.access_support import ENV_A, ENV_B, container_id
from tests.docker_engine.fake_engine import error_reply
from tests.docker_engine.swarm_engine import MANAGER, plain_spec
from tests.services.conftest import ContainerRig, SwarmRig
from tests.support import present

CANDIDATE = container_id(0xD0C)
SOMEONE_ELSE = container_id(0xE1)
HOSTNAME = CANDIDATE[:12]
PROJECT = "dockmin-stack"
SERVICE = "s" * 25
NODE = "n" * 25
NODE_LABEL = "com.docker.swarm.node.id"
SERVICE_LABEL = "com.docker.swarm.service.id"
COMPOSE_LABEL = "com.docker.compose.project"


def probe_of(database: Any, runtime: Any, engine: Any) -> EngineSelfProbe:
    state = SimpleNamespace(database=database, auth=runtime, engine=engine)
    return EngineSelfProbe(SimpleNamespace(state=state))  # type: ignore[arg-type]


@pytest.fixture
def probe(rig: ContainerRig) -> EngineSelfProbe:
    return probe_of(rig.database, rig.runtime, rig.engine)


def add_self(rig: ContainerRig, **config: Any) -> None:
    rig.host.add(
        CANDIDATE,
        "dockmin",
        config={"Image": "dockmin:1", "Hostname": HOSTNAME, "Labels": {}, **config},
        mounts=[
            {"Type": "volume", "Name": "dockmin-data", "Destination": "/data", "RW": True},
            {"Type": "bind", "Source": "/srv/key", "Destination": "/key", "RW": False},
            {"Type": "volume", "Name": "", "Destination": "/anonymous", "RW": True},
        ],
    )


class TestEnvironments:
    async def test_every_environment_is_listed_with_its_connection_kind(
        self, probe: EngineSelfProbe
    ) -> None:
        refs = await probe.environments()

        assert sorted(refs, key=lambda ref: ref.environment_id) == [
            EnvironmentRef(ENV_A, "tcp_tls", False),
            EnvironmentRef(ENV_B, "unix_socket", False),
        ]

    async def test_a_swarm_environment_is_marked(self, swarm: SwarmRig) -> None:
        refs = await probe_of(swarm.database, swarm.runtime, swarm.engine).environments()

        assert {ref.environment_id: ref.is_swarm for ref in refs} == {ENV_A: True, ENV_B: True}


class TestInspect:
    async def test_the_container_is_described_by_its_volumes_and_labels(
        self, rig: ContainerRig, probe: EngineSelfProbe
    ) -> None:
        add_self(rig, Labels={COMPOSE_LABEL: PROJECT, SERVICE_LABEL: SERVICE, NODE_LABEL: NODE})

        found = await probe.inspect(EnvironmentRef(ENV_B, "unix_socket"), CANDIDATE)

        assert found == ContainerFacts(
            container_id=CANDIDATE,
            name="dockmin",
            hostname=HOSTNAME,
            volumes=frozenset({"dockmin-data"}),
            compose_project=PROJECT,
            swarm_service_id=SERVICE,
            swarm_node_id=NODE,
            mount_sources=frozenset({"/srv/key"}),
        )

    async def test_a_container_without_labels_has_none_of_them(
        self, rig: ContainerRig, probe: EngineSelfProbe
    ) -> None:
        add_self(rig)

        found = await probe.inspect(EnvironmentRef(ENV_B, "unix_socket"), CANDIDATE)

        assert (
            present(found).compose_project,
            present(found).swarm_service_id,
            present(found).swarm_node_id,
        ) == (
            None,
            None,
            None,
        )

    async def test_an_environment_without_the_container_answers_none_and_asks_for_no_tasks(
        self, rig: ContainerRig, probe: EngineSelfProbe
    ) -> None:
        found = await probe.inspect(EnvironmentRef(ENV_B, "unix_socket"), CANDIDATE)

        assert found is None
        assert rig.host.calls("GET", "/tasks") == []

    async def test_a_container_whose_name_is_the_candidate_but_whose_id_differs_is_refused(
        self, rig: ContainerRig, probe: EngineSelfProbe
    ) -> None:
        rig.host.add(SOMEONE_ELSE, CANDIDATE)

        found = await probe.inspect(EnvironmentRef(ENV_B, "unix_socket"), CANDIDATE)

        assert found is None


class TestSwarmTasks:
    async def test_a_running_task_of_the_exact_container_id_stands_in_for_the_container(
        self, swarm: SwarmRig
    ) -> None:
        service_id = swarm.host.add_service(plain_spec("dockmin"))
        task = swarm.host.tasks[swarm.host.service_tasks(service_id)[0]]
        task_container = task["Status"]["ContainerStatus"]["ContainerID"]
        probe = probe_of(swarm.database, swarm.runtime, swarm.engine)

        found = await probe.inspect(EnvironmentRef(ENV_A, "tcp_tls", is_swarm=True), task_container)

        assert found is not None
        assert found.hostname is None
        assert found.swarm_service_id == service_id
        assert found.swarm_node_id == task["NodeID"] == MANAGER

    async def test_a_swarm_with_no_task_of_that_id_answers_none(self, swarm: SwarmRig) -> None:
        swarm.host.add_service(plain_spec("dockmin"))
        probe = probe_of(swarm.database, swarm.runtime, swarm.engine)

        found = await probe.inspect(EnvironmentRef(ENV_A, "tcp_tls", is_swarm=True), CANDIDATE)

        assert found is None

    async def test_a_node_that_is_not_a_manager_answers_none_rather_than_failing(
        self, swarm: SwarmRig
    ) -> None:
        swarm.host.outage.add("tasks")
        probe = probe_of(swarm.database, swarm.runtime, swarm.engine)

        found = await probe.inspect(EnvironmentRef(ENV_A, "tcp_tls", is_swarm=True), CANDIDATE)

        assert found is None

    async def test_any_other_engine_failure_listing_tasks_is_raised(self, swarm: SwarmRig) -> None:
        swarm.host.route("GET", "/tasks", error_reply(500))
        probe = probe_of(swarm.database, swarm.runtime, swarm.engine)

        with pytest.raises(EngineError):
            await probe.inspect(EnvironmentRef(ENV_A, "tcp_tls", is_swarm=True), CANDIDATE)
