"""Swarm services, stacks and nodes that run the container dockmin runs in."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound, SelfProtected
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    StackServices,
    SwarmServices,
    build_access_services,
    build_host_clients,
    build_stack_services,
    build_swarm_services,
)
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_deploy import file_digest
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.docker_engine.swarm_engine import MANAGER, WORKER, plain_spec
from tests.self_support import Held, home_identity, self_protected_rows
from tests.services.conftest import SwarmRig
from tests.stack_support import Rig
from tests.swarm_stack_support import (
    NAME,
    StackDaemon,
    create_stack,
    deploy_stack,
    shop_file,
)

STALE_VERSION = 1
UNRELATED_SERVICE = "u" * 25
SERVICE_REFUSAL = str(SelfProtected.service())
STACK_REFUSAL = str(SelfProtected.stack(NAME))

type ServiceCall = Callable[[SwarmServices, str], Awaitable[Any]]

SERVICE_OPERATIONS: dict[str, tuple[ServiceCall, str]] = {
    "update": (
        lambda s, sid: s.service_actions.update(
            ADMIN, ENV_A, sid, {"name": "web", "image": "busybox:2"}, expected_version=STALE_VERSION
        ),
        "service_updated",
    ),
    "scale": (
        lambda s, sid: s.service_actions.scale(
            ADMIN, ENV_A, sid, replicas=3, expected_version=STALE_VERSION, expected_replicas=1
        ),
        "service_scaled",
    ),
    "force_update": (
        lambda s, sid: s.service_actions.force_update(
            ADMIN, ENV_A, sid, expected_version=STALE_VERSION
        ),
        "service_force_updated",
    ),
    "rollback": (
        lambda s, sid: s.service_actions.rollback(
            ADMIN, ENV_A, sid, expected_version=STALE_VERSION
        ),
        "service_rolled_back",
    ),
    "remove": (
        lambda s, sid: s.service_actions.remove(
            ADMIN, ENV_A, sid, expected_name="web", expected_version=STALE_VERSION
        ),
        "service_removed",
    ),
}


def guarded_swarm(rig: SwarmRig, held: Held) -> SwarmServices:
    return build_swarm_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.registry.registries,
        rig.access,
        rig.inspector,
        rig.recorder,
        self_identity=held,
    )


def guarded_stacks(rig: Rig, held: Held) -> StackServices:
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_stack_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
        self_identity=held,
    )


@pytest.fixture
def own_service(swarm: SwarmRig) -> tuple[SwarmServices, str, str]:
    """Dockmin's service and another one on the manager, with services guarded for the first."""
    own = swarm.host.add_service(plain_spec("web"))
    other = swarm.host.add_service(plain_spec("other"))
    held = Held(home_identity(swarm_service=own, swarm_node=MANAGER))
    return guarded_swarm(swarm, held), own, other


@pytest.mark.parametrize("operation", list(SERVICE_OPERATIONS))
async def test_a_change_to_the_service_dockmin_runs_as_is_refused_with_a_row_and_no_engine_write(
    swarm: SwarmRig, own_service: tuple[SwarmServices, str, str], operation: str
) -> None:
    services, own, _ = own_service
    call, event = SERVICE_OPERATIONS[operation]

    with pytest.raises(SelfProtected) as refused:
        await call(services, own)

    [row] = await self_protected_rows(swarm.db)
    assert str(refused.value) == SERVICE_REFUSAL
    assert (row["action"], row["target_id"], row["environment_id"]) == (event, own, ENV_A)
    assert row["detail"]["message"] == SERVICE_REFUSAL
    assert swarm.host.mutating() == []
    assert len(swarm.engine.locks) == 0


async def test_another_service_is_scaled_while_dockmins_service_is_refused(
    swarm: SwarmRig, own_service: tuple[SwarmServices, str, str]
) -> None:
    services, _, other = own_service
    version = (await services.services.detail(ADMIN, ENV_A, other)).version

    outcome = await services.service_actions.scale(
        ADMIN, ENV_A, other, replicas=2, expected_version=version, expected_replicas=1
    )

    assert outcome.replicas_after == 2
    assert await self_protected_rows(swarm.db) == []


async def test_a_user_who_cannot_see_the_service_gets_the_not_found_any_hidden_service_gives(
    swarm: SwarmRig, own_service: tuple[SwarmServices, str, str]
) -> None:
    services, own, other = own_service

    with pytest.raises(ResourceNotFound) as hidden:
        await services.service_actions.remove(
            STANDARD, ENV_A, other, expected_name="other", expected_version=STALE_VERSION
        )
    with pytest.raises(ResourceNotFound) as protected:
        await services.service_actions.remove(
            STANDARD, ENV_A, own, expected_name="web", expected_version=STALE_VERSION
        )

    assert str(protected.value) == str(hidden.value)
    assert await self_protected_rows(swarm.db) == []
    assert swarm.host.mutating() == []


class TestNodeDrain:
    async def drain(self, services: SwarmServices, rig: SwarmRig, node: str, to: str) -> Any:
        document = rig.host.nodes[node]
        return await services.nodes.set_availability(
            ADMIN,
            ENV_A,
            node,
            availability=to,
            expected_version=document["Version"]["Index"],
            expected_availability=document["Spec"]["Availability"],
        )

    async def test_draining_the_node_that_runs_dockmin_is_refused_and_nothing_is_sent(
        self, swarm: SwarmRig, own_service: tuple[SwarmServices, str, str]
    ) -> None:
        services, _, _ = own_service

        with pytest.raises(SelfProtected) as refused:
            await self.drain(services, swarm, MANAGER, "drain")

        [row] = await self_protected_rows(swarm.db)
        assert str(refused.value) == str(SelfProtected.node())
        assert row["target_id"] == MANAGER
        assert swarm.host.mutating() == []
        assert swarm.host.nodes[MANAGER]["Spec"]["Availability"] == "active"

    @pytest.mark.parametrize(
        ("node", "availability"),
        [(MANAGER, "pause"), (WORKER, "drain")],
        ids=["pausing-its-node", "draining-another-node"],
    )
    async def test_other_changes_to_availability_are_allowed(
        self,
        swarm: SwarmRig,
        own_service: tuple[SwarmServices, str, str],
        node: str,
        availability: str,
    ) -> None:
        services, _, _ = own_service

        await self.drain(services, swarm, node, availability)

        assert swarm.host.nodes[node]["Spec"]["Availability"] == availability
        assert await self_protected_rows(swarm.db) == []


class TestSwarmStacks:
    @pytest.fixture
    async def deployed(self, swarm_stack_rig: Rig, stack_daemon: StackDaemon) -> Rig:
        await create_stack(swarm_stack_rig)
        view = await deploy_stack(swarm_stack_rig)
        assert view.state == "succeeded", view.outcome
        return swarm_stack_rig

    async def change_stored_file(self, rig: Rig) -> None:
        text = shop_file(web_replicas=3)
        await rig.db.execute(
            "UPDATE stack SET compose_file = ?, compose_file_digest = ? WHERE kind = 'swarm'",
            (text, file_digest(text)),
        )

    async def test_removing_a_stack_that_includes_dockmins_service_is_refused_and_removes_nothing(
        self, deployed: Rig, stack_daemon: StackDaemon
    ) -> None:
        held = Held(home_identity(swarm_service=stack_daemon.service_id("Shop_web")))
        review = deployed.fresh()
        row = await review.swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await review.swarm_planner.remove_plan(ADMIN, ENV_A, row)
        before = stack_daemon.mutations()

        with pytest.raises(SelfProtected) as refused:
            await guarded_stacks(deployed, held).swarm_actions.remove(
                ADMIN, ENV_A, NAME, stack_id=row.id, expected_members=plan.digest
            )

        [failure] = await self_protected_rows(deployed.db)
        assert str(refused.value) == STACK_REFUSAL
        assert failure["action"] == "stack_removed"
        assert stack_daemon.mutations() == before
        assert "Shop_web" in stack_daemon.service_names()

    async def test_a_redeploy_that_would_change_dockmins_service_is_refused_and_changes_nothing(
        self, deployed: Rig, stack_daemon: StackDaemon
    ) -> None:
        held = Held(home_identity(swarm_service=stack_daemon.service_id("Shop_web")))
        await self.change_stored_file(deployed)
        review = deployed.fresh()
        row = await review.swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await review.swarm_planner.deploy_plan(
            ADMIN, ENV_A, row, remove_orphans=False, resolve_images=True
        )
        before = stack_daemon.mutations()

        with pytest.raises(SelfProtected):
            await guarded_stacks(deployed, held).swarm_actions.redeploy(
                ADMIN,
                ENV_A,
                NAME,
                expected_file_digest=row.compose_file_digest,
                expected_inputs_digest=row.inputs_digest or "",
                expected_plan=plan.digest,
            )

        [failure] = await self_protected_rows(deployed.db)
        assert failure["action"] == "stack_redeployed"
        assert stack_daemon.mutations() == before
        assert stack_daemon.service("Shop_web")["Mode"]["Replicated"]["Replicas"] == 2

    async def test_a_redeploy_of_a_stack_that_does_not_include_dockmins_service_goes_ahead(
        self, deployed: Rig, stack_daemon: StackDaemon
    ) -> None:
        held = Held(home_identity(swarm_service=UNRELATED_SERVICE))
        await self.change_stored_file(deployed)
        review = deployed.fresh()
        row = await review.swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await review.swarm_planner.deploy_plan(
            ADMIN, ENV_A, row, remove_orphans=False, resolve_images=True
        )

        outcome = await guarded_stacks(deployed, held).swarm_actions.redeploy(
            ADMIN,
            ENV_A,
            NAME,
            expected_file_digest=row.compose_file_digest,
            expected_inputs_digest=row.inputs_digest or "",
            expected_plan=plan.digest,
        )

        assert "service:Shop_web" in outcome.updated
        assert await self_protected_rows(deployed.db) == []
