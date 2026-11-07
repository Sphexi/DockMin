"""container-updater when its run is cancelled while an update is being verified."""

import asyncio
from typing import Any

import pytest

from app.services import container_recreate
from app.services.container_recreate import ContainerRecreateService
from tests.automation_support import counts
from tests.docker_engine.container_engine import created_id
from tests.provisioning_support import OLD
from tests.updater_support import UPDATER, World, container

NEW = created_id(1)
ROLLED_BACK = "container_update_rolled_back"
RECREATED = "container_recreated"
SHORT_WAIT = 0.05
HELD_RUN_SECONDS = 10
NORMAL_KEYS = {
    "name",
    "reference",
    "image_before",
    "image_after",
    "digest_after",
    "failed_container_id",
    "stage",
    "verify_failure",
}
RULE_KEYS = {"rule_id", "rule_name"}
WATCHED = {"action": "update", "verify_seconds": 60}
HEALTHCHECK = {
    "Test": ["CMD", "true"],
    "Interval": 5_000_000_000,
    "Timeout": 1_000_000_000,
    "StartPeriod": 0,
}


class CrashesNewContainer:
    """Fails the replacement on its first inspection, after an optional action on the run."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.run: asyncio.Future[Any] | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_first = False
        world.host.on("inspect", self)

    async def __call__(self) -> None:
        box = self.world.host.boxes.get(NEW)
        if box is None or box.state != "running":
            return
        if self.hold_first:
            self.entered.set()
            await self.release.wait()
        box.state = "exited"
        box.exit_code = 1
        if self.run is not None and not self.hold_first:
            self.run.cancel()


class WatchScript:
    """Stands in for the verification wait: marks the replacement, then stalls until interrupted."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch, health: str) -> None:
        self.world = world
        self.health = health
        self.waits = 0
        self.stalling = True
        self.stalled = asyncio.Event()
        world.host.boxes[OLD].config["Healthcheck"] = HEALTHCHECK
        monkeypatch.setattr(ContainerRecreateService, "_pause", self._pause)

    async def _pause(self, seconds: float, interrupt: asyncio.Event) -> None:
        self.waits += 1
        for box in self.world.host.boxes.values():
            if box.id != OLD:
                box.health = self.health
        if self.stalling and self.waits > 1:
            self.stalled.set()
            await interrupt.wait()


def start_run(world: World, job: int) -> asyncio.Future[Any]:
    return asyncio.ensure_future(world.run(job))


async def settle_background_work() -> None:
    for _ in range(2):
        await asyncio.gather(
            *(asyncio.all_tasks() - {asyncio.current_task()}), return_exceptions=True
        )


async def test_a_run_cancelled_while_verification_fails_rolls_back_and_writes_one_rollback_row(
    world: World,
) -> None:
    world.new_image(3)
    crash = CrashesNewContainer(world)
    rule_id, job = await world.rule(container("web"))
    crash.run = start_run(world, job)

    with pytest.raises(asyncio.CancelledError):
        await crash.run

    [failure] = world.rows(RECREATED)
    [rolled] = world.rows(ROLLED_BACK)
    assert (world.host.boxes[OLD].name, world.host.boxes[OLD].state) == ("web", "running")
    assert NEW not in world.host.boxes
    assert (failure["result"], failure["detail"]["interrupted"]) == ("failure", True)
    assert rolled["detail"].keys() - RULE_KEYS == NORMAL_KEYS | {"interrupted"}
    assert rolled["detail"]["interrupted"] is True
    assert "settled_late" not in rolled["detail"]
    assert (rolled["actor_subsystem"], rolled["rule_id"], rolled["target_id"]) == (
        UPDATER,
        rule_id,
        OLD,
    )


async def test_the_next_run_against_the_same_digest_is_held_and_recreates_nothing(
    world: World,
) -> None:
    world.new_image(3)
    crash = CrashesNewContainer(world)
    _, job = await world.rule(container("web"))
    crash.run = start_run(world, job)
    with pytest.raises(asyncio.CancelledError):
        await crash.run
    mutations_before = len(world.host.mutations())
    world.advance(HELD_RUN_SECONDS)

    result = await world.run(job)

    assert counts(result)["held"] == 1
    assert len(world.host.mutations()) == mutations_before
    assert len(world.rows(RECREATED)) == 1


async def test_a_sequence_settling_after_the_wait_writes_a_late_failure_row_and_one_rollback_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_recreate, "settle_seconds", lambda *_: SHORT_WAIT)
    world.new_image(3)
    crash = CrashesNewContainer(world)
    crash.hold_first = True
    _, job = await world.rule(container("web"))
    run = start_run(world, job)
    await crash.entered.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert world.rows(ROLLED_BACK) == []
    crash.release.set()
    await settle_background_work()

    first, late = world.rows(RECREATED)
    [rolled] = world.rows(ROLLED_BACK)
    assert first["detail"]["reason"] == "outcome_unknown"
    assert (late["result"], late["detail"]["settled_late"]) == ("failure", True)
    assert rolled["detail"].keys() - RULE_KEYS == NORMAL_KEYS | {"interrupted", "settled_late"}
    assert world.host.boxes[OLD].state == "running"


async def test_a_run_cancelled_outside_verification_writes_no_rollback_row(world: World) -> None:
    world.new_image(3)
    stop = world.host.hold("stop")
    _, job = await world.rule(container("web"))
    run = start_run(world, job)
    await stop.entered.wait()

    run.cancel()
    stop.release.set()
    with pytest.raises(asyncio.CancelledError):
        await run

    [recreated] = world.rows(RECREATED)
    assert (recreated["result"], recreated["detail"]["interrupted"]) == ("success", True)
    assert world.rows(ROLLED_BACK) == []


async def test_a_failed_update_whose_rollback_also_fails_writes_no_rollback_row(
    world: World,
) -> None:
    world.new_image(3)
    world.host.fail("start", 500, target=NEW)
    world.host.fail("rename", 500, skip=1)
    _, job = await world.rule(container("web"))

    result = await world.run(job)

    [failure] = world.rows(RECREATED)
    assert result.outcome == "failure"
    assert failure["detail"]["rollback_failed"] is True
    assert world.rows(ROLLED_BACK) == []
    assert "rolled_back" not in counts(result)


async def test_a_cancel_while_the_replacement_reports_unhealthy_rolls_back_and_holds_the_digest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.new_image(3)
    watch = WatchScript(world, monkeypatch, "unhealthy")
    _, job = await world.rule(container("web"), WATCHED)
    run = start_run(world, job)
    await watch.stalled.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    [failure] = world.rows(RECREATED)
    [rolled] = world.rows(ROLLED_BACK)
    assert (world.host.boxes[OLD].name, world.host.boxes[OLD].state) == ("web", "running")
    assert NEW not in world.host.boxes
    assert (failure["result"], failure["detail"]["interrupted"]) == ("failure", True)
    assert (rolled["detail"]["interrupted"], rolled["detail"]["verify_failure"]) == (
        True,
        "unhealthy",
    )
    world.advance(HELD_RUN_SECONDS)
    result = await world.run(job)
    assert counts(result)["held"] == 1
    assert len(world.rows(RECREATED)) == 1


async def test_a_cancel_before_verification_saw_a_failure_rolls_back_and_the_next_run_retries(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.new_image(3)
    watch = WatchScript(world, monkeypatch, "starting")
    _, job = await world.rule(container("web"), WATCHED)
    run = start_run(world, job)
    await watch.stalled.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    [failure] = world.rows(RECREATED)
    [rolled] = world.rows(ROLLED_BACK)
    assert (world.host.boxes[OLD].name, world.host.boxes[OLD].state) == ("web", "running")
    assert NEW not in world.host.boxes
    assert (failure["result"], failure["detail"]["interrupted"]) == ("failure", True)
    assert rolled["detail"]["verify_failure"] == "interrupted"
    watch.stalling = False
    watch.health = "healthy"
    world.advance(HELD_RUN_SECONDS)
    result = await world.run(job)
    assert "held" not in counts(result)
    assert [row["result"] for row in world.rows(RECREATED)] == ["failure", "success"]
