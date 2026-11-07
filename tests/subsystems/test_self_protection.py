"""Automation leaves the container dockmin runs in alone: counted as skipped, nothing recorded."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.container_updater import ContainerUpdater
from app.subsystems.stack_restarter import StackRestarter
from app.subsystems.webhook_receiver import WebhookReceiver
from tests.access_support import ENV_A
from tests.automation_support import Subject, cid, counts
from tests.provisioning_support import OLD
from tests.self_support import Held, home_identity
from tests.stack_support import Rig as StackRig
from tests.stack_support import project_box, read_records, seed_stack_row
from tests.swarm_updater_support import SERVICE, STACK, UPDATE, Built, Shop
from tests.updater_support import SHOP, World, container, member, stack_selector

SELF_PROTECTED = "self_protected"
EVERYTHING = {"kind": "environment"}
SHOP_NAME = "shop"
DB_BOX, WEB_BOX = 1, 2
WEB_SERVICE = "Shop_web"
REDEPLOY = {"action": "redeploy", "verify_seconds": 0}


def restart_target(name: str) -> dict[str, object]:
    return {"kind": "container", "names": [name]}


def with_identity(subject: Subject, rig: Any, held: Held) -> None:
    """Give the subject a container-updater that reads the identity the test holds."""
    registries = rig.registry.registries
    deps = dataclasses.replace(subject.deps, registries=registries)
    subject.subsystem = ContainerUpdater(deps, self_identity=held)


class TestHealthChecker:
    @pytest.fixture
    def checker(self, container_rig: Any, settings: Settings) -> Subject:
        held = Held(home_identity(container=cid(1)))
        return Subject(
            container_rig,
            settings,
            lambda deps: ContainerHealthChecker(deps, self_identity=held),
        )

    async def test_dockmins_unhealthy_container_is_skipped_and_the_others_are_restarted(
        self, checker: Subject
    ) -> None:
        checker.host.add(cid(1), "dockmin", health="unhealthy")
        checker.host.add(cid(2), "web", health="unhealthy")
        _, job = await checker.rule(EVERYTHING)

        result = await checker.run(job)

        assert checker.restarted() == [cid(2)]
        assert counts(result) == {
            "restarted": 1,
            "matched": 2,
            "unhealthy": 1,
            SELF_PROTECTED: 1,
        }
        assert {event["target_id"] for event in checker.subsystem_events()} == {cid(2)}

    async def test_a_rule_that_names_dockmins_container_restarts_nothing_and_writes_no_row(
        self, checker: Subject
    ) -> None:
        checker.host.add(cid(1), "dockmin", health="unhealthy")
        _, job = await checker.rule(restart_target("dockmin"))

        result = await checker.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert checker.host.mutations() == []
        assert checker.subsystem_events() == []


class TestStackRestarter:
    @pytest.fixture
    def record(self, tmp_path: Path) -> Path:
        return tmp_path / "compose-record.jsonl"

    def restarter_with(self, stack_rig: StackRig, settings: Settings, held: Held) -> Subject:
        return Subject(stack_rig, settings, lambda deps: StackRestarter(deps, self_identity=held))

    async def seed(self, subject: Subject, record: Path) -> None:
        text = (
            "services:\n  db:\n    image: nginx:1\n  web:\n    image: nginx:1\n"
            f"x-fake:\n  record: {record}\n"
        )
        await seed_stack_row(subject.rig, ENV_A, SHOP_NAME, text)
        project_box(subject.rig.host, DB_BOX, SHOP_NAME, "db")
        project_box(subject.rig.host, WEB_BOX, SHOP_NAME, "web")

    @pytest.mark.parametrize(
        ("held", "selector"),
        [
            pytest.param(
                Held(home_identity(compose_project=SHOP_NAME)),
                {"kind": "stack", "names": [SHOP_NAME]},
                id="stack-named-for-dockmin",
            ),
            pytest.param(
                Held(home_identity(container=f"{0xA0000 + WEB_BOX:064x}")),
                {"kind": "stack", "names": [SHOP_NAME]},
                id="stack-holding-dockmin",
            ),
            pytest.param(
                Held(home_identity(container=f"{0xA0000 + WEB_BOX:064x}")),
                {"kind": "container", "names": [f"{SHOP_NAME}-web-{WEB_BOX}"]},
                id="container-dockmin-runs-in",
            ),
        ],
    )
    async def test_a_restart_aimed_at_dockmin_is_skipped_and_compose_never_runs(
        self,
        stack_rig: StackRig,
        settings: Settings,
        record: Path,
        held: Held,
        selector: dict[str, str],
    ) -> None:
        restarter = self.restarter_with(stack_rig, settings, held)
        await self.seed(restarter, record)
        _, job = await restarter.rule(selector)

        result = await restarter.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert read_records(record) == []
        assert restarter.host.mutations() == []
        assert restarter.subsystem_events() == []


class TestContainerUpdater:
    async def test_dockmins_container_is_left_out_of_an_update(self, world: World) -> None:
        with_identity(world.subject, world.rig, Held(home_identity(container=OLD)))
        _, job = await world.rule(container("web"))

        result = await world.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert world.rows() == []
        assert world.verbs() == []

    @pytest.mark.parametrize(
        "identity",
        [
            pytest.param(lambda world: home_identity(compose_project=SHOP), id="named-for-stack"),
            pytest.param(
                lambda world: home_identity(container=member(world, 1)), id="holds-a-member"
            ),
        ],
    )
    async def test_a_compose_stack_that_holds_dockmin_is_left_out_of_an_update(
        self, shop: World, identity: Any
    ) -> None:
        with_identity(shop.subject, shop.rig, Held(identity(shop)))
        _, job = await shop.rule(stack_selector())

        result = await shop.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert shop.rows() == []
        assert shop.verbs() == []

    async def test_a_swarm_service_dockmin_runs_as_is_left_out_of_an_update(
        self, swarm_web: Built
    ) -> None:
        with_identity(
            swarm_web.subject,
            swarm_web.rig,
            Held(home_identity(swarm_service=swarm_web.sid)),
        )
        _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

        result = await swarm_web.subject.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert swarm_web.host.mutating() == []
        assert swarm_web.subject.subsystem_events() == []

    async def test_a_swarm_stack_that_holds_dockmins_service_is_left_out_of_an_update(
        self, swarm_shop: Shop
    ) -> None:
        shop = swarm_shop
        shop.move()
        held = Held(home_identity(swarm_service=shop.member_id(WEB_SERVICE)))
        with_identity(shop.subject, shop.rig, held)
        sent = shop.sent()
        _, job = await shop.subject.rule(STACK, UPDATE)

        result = await shop.subject.run(job)

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert shop.sent() == sent
        assert shop.subject.subsystem_events() == []


class TestWebhookReceiver:
    def receiving(self, world: World, held: Held) -> None:
        deps = dataclasses.replace(world.subject.deps, registries=world.rig.registry.registries)
        world.subject.subsystem = WebhookReceiver(deps, self_identity=held)
        world.subject.name = WebhookReceiver.name

    async def test_dockmins_container_is_left_out_of_a_redeploy(self, world: World) -> None:
        self.receiving(world, Held(home_identity(container=OLD)))
        _, job = await world.rule(container("web"), REDEPLOY)

        result = await world.subject.run(job, trigger="webhook")

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert world.rows() == [] and world.verbs() == []

    async def test_a_compose_stack_named_for_dockmin_is_left_out_of_a_redeploy(
        self, shop: World
    ) -> None:
        self.receiving(shop, Held(home_identity(compose_project=SHOP)))
        _, job = await shop.rule(stack_selector(), REDEPLOY)

        result = await shop.subject.run(job, trigger="webhook")

        assert (result.outcome, result.attempted) == ("success", 0)
        assert counts(result)[SELF_PROTECTED] == 1
        assert shop.rows() == [] and shop.verbs() == []
