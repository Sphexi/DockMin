"""container-updater on a recreated container that others share the network of."""

from typing import Any

import pytest

from app.services.container_actions import ContainerActionService
from app.services.network_joiners import MAX_JOINER_CHAIN
from tests.access_support import ENV_A
from tests.automation_support import counts
from tests.docker_engine.container_engine import created_id
from tests.image_support import REFERENCE
from tests.provisioning_support import OLD, old_body
from tests.updater_support import World, container

BY_ID = f"{0xC1:064x}"
BY_NAME = f"{0xC2:064x}"
STOPPED = f"{0xC3:064x}"
GRANDCHILD = f"{0xC4:064x}"
NEW_OWNER = created_id(1)
PROJECT_LABEL = "com.docker.compose.project"


def joiner(world: World, identifier: str, name: str, mode: str, state: str = "running") -> None:
    network_id = next(iter(world.host.networks))
    body = old_body(
        network_id, config={"Image": REFERENCE, "NetworkingConfig": {}}, host={"NetworkMode": mode}
    )
    world.host.add_configured(identifier, name, body, state)


def network_mode_of(world: World, container_id: str) -> str:
    return str(world.host.boxes[container_id].inspect()["HostConfig"]["NetworkMode"])


def recreated_rows(world: World) -> dict[str, dict[str, Any]]:
    return {row["target_id"]: row for row in world.rows("container_recreated")}


async def test_recreating_an_owner_recreates_id_joiners_and_restarts_name_joiners(
    world: World,
) -> None:
    joiner(world, BY_ID, "by-id", f"container:{OLD}")
    joiner(world, BY_NAME, "by-name", "container:web")
    joiner(world, STOPPED, "stopped-id", f"container:{OLD}", state="exited")
    rule_id, job = await world.rule(container("web"))

    result = await world.run(job)

    renewed = {box.name: box for box in world.host.boxes.values()}
    detail = counts(result)
    rows = recreated_rows(world)
    owner_row = rows[OLD]
    assert result.outcome == "success"
    assert (detail["updated"], detail["joiners_recreated"], detail["joiners_restarted"]) == (
        1,
        2,
        1,
    )
    assert BY_ID not in world.host.boxes and STOPPED not in world.host.boxes
    assert BY_NAME in world.host.boxes
    assert network_mode_of(world, renewed["by-id"].id) == f"container:{NEW_OWNER}"
    assert renewed["by-id"].state == "running"
    assert network_mode_of(world, renewed["stopped-id"].id) == f"container:{NEW_OWNER}"
    assert renewed["stopped-id"].state != "running"
    assert network_mode_of(world, BY_NAME) == "container:web"
    assert any(row["target_id"] == BY_NAME for row in world.rows("container_restarted"))
    assert owner_row["detail"]["joiners_recreate"] == "by-id, stopped-id"
    assert owner_row["detail"]["joiners_restart"] == "by-name"
    for target in (BY_ID, STOPPED):
        assert (rows[target]["actor_subsystem"], rows[target]["rule_id"]) == (
            "container-updater",
            rule_id,
        )
        assert rows[target]["detail"]["joined"] == "web"
    assert {row["environment_id"] for row in world.rows()} == {ENV_A}


async def test_a_joiner_that_cannot_be_recreated_fails_the_run_with_its_reason(
    world: World,
) -> None:
    joiner(world, BY_ID, "by-id", f"container:{OLD}")
    joiner(world, BY_NAME, "by-name", "container:web")
    network_id = next(iter(world.host.networks))
    managed = old_body(
        network_id,
        config={"Image": REFERENCE, "NetworkingConfig": {}, "Labels": {PROJECT_LABEL: "shop"}},
        host={"NetworkMode": f"container:{OLD}"},
    )
    world.host.add_configured(STOPPED, "managed", managed, "running")
    world.host.fail("stop", 500, target=BY_ID)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    rows = recreated_rows(world)
    assert (result.outcome, result.attempted) == ("partial", 3)
    assert counts(result)["joiners_restarted"] == 1
    assert rows[BY_ID]["result"] == "failure" and rows[STOPPED]["result"] == "failure"
    assert rows[STOPPED]["detail"]["reason"] == "compose_managed"
    assert rows[BY_ID]["detail"]["reason"]
    assert rows[OLD]["result"] == "success"
    assert network_mode_of(world, BY_ID) == f"container:{OLD}"


async def test_the_joiners_of_a_recreated_joiner_follow_it_onto_its_new_container(
    world: World,
) -> None:
    joiner(world, BY_ID, "by-id", f"container:{OLD}")
    joiner(world, GRANDCHILD, "grandchild", f"container:{BY_ID}")
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    boxes = {box.name: box for box in world.host.boxes.values()}
    assert counts(result)["joiners_recreated"] == 2
    assert network_mode_of(world, boxes["by-id"].id) == f"container:{NEW_OWNER}"
    assert network_mode_of(world, boxes["grandchild"].id) == f"container:{boxes['by-id'].id}"
    assert recreated_rows(world)[GRANDCHILD]["detail"]["joined"] == "by-id"


@pytest.mark.parametrize("joiners", [0, 1])
async def test_the_owner_is_only_waited_on_when_something_follows_it(
    world: World, monkeypatch: pytest.MonkeyPatch, joiners: int
) -> None:
    waited: list[str] = []
    real = ContainerActionService.owner_ready

    async def spy(self: ContainerActionService, client: Any, container_id: str, *rest: Any) -> Any:
        waited.append(container_id)
        return await real(self, client, container_id, *rest)

    monkeypatch.setattr(ContainerActionService, "owner_ready", spy)
    if joiners:
        joiner(world, BY_NAME, "by-name", "container:web")
    _, job = await world.rule(container("web"))

    await world.run(job)

    assert waited == [NEW_OWNER] * joiners


async def test_a_chain_past_the_limit_is_followed_to_the_limit_and_the_rest_are_counted(
    world: World,
) -> None:
    extra = 2
    for number in range(MAX_JOINER_CHAIN + extra):
        joiner(world, f"{0x2000 + number:064x}", f"n{number:03d}", "container:web")
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    detail = counts(result)
    assert (detail["joiners_restarted"], detail["joiners_omitted"]) == (MAX_JOINER_CHAIN, extra)
