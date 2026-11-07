"""container-updater records the images a stack redeploy replaced and removes them later."""

from typing import Any

import pytest

from tests.automation_support import counts
from tests.docker_engine.container_engine import Box
from tests.image_support import full_id, image_id
from tests.updater_support import SHOP, World, member, stack_selector

GRACE_HOURS = 1
GRACE_SECONDS = GRACE_HOURS * 3600
PRUNING = {
    "action": "update",
    "verify_seconds": 0,
    "prune_replaced": True,
    "prune_after_hours": GRACE_HOURS,
}
REPLACED = 1
CURRENT = 2
KEPT = 3
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


async def records(world: World) -> list[dict[str, str]]:
    found = await world.db.fetch_limited(
        "SELECT image_id, target_type, target_id FROM replaced_image ORDER BY image_id",
        (),
        limit=1000,
    )
    return [dict(row) for row in found]


def project_boxes(world: World) -> list[Box]:
    return [box for box in world.host.boxes.values() if box.labels.get(PROJECT_LABEL) == SHOP]


def redeploy_leaves(world: World, monkeypatch: pytest.MonkeyPatch, **images: str) -> None:
    """Make each redeploy leave the named services on the given image ids."""
    sim = world.rig.sim
    original = sim.apply_up

    def apply_up(name: str, resolved: Any, remove_orphans: bool) -> None:
        original(name, resolved, remove_orphans)
        for box in project_boxes(world):
            box.image_id = images[box.labels[SERVICE_LABEL]]

    monkeypatch.setattr(sim, "apply_up", apply_up)


@pytest.fixture
async def pruning_shop(shop: World) -> tuple[int, int]:
    return await shop.rule(stack_selector(), PRUNING)


async def test_a_redeploy_records_only_the_images_no_container_of_the_stack_runs_afterwards(
    shop: World, pruning_shop: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    shop.host.boxes[member(shop, 2)].image_id = full_id(KEPT)
    redeploy_leaves(shop, monkeypatch, web=full_id(CURRENT), worker=full_id(KEPT))

    result = await shop.run(pruning_shop[1])

    assert counts(result)["updated"] == 1
    assert await records(shop) == [
        {"image_id": image_id(REPLACED), "target_type": "stack", "target_id": SHOP}
    ]


async def test_a_recorded_stack_image_is_removed_once_the_grace_period_has_passed(
    shop: World, pruning_shop: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    rule_id, job = pruning_shop
    redeploy_leaves(shop, monkeypatch, web=full_id(CURRENT), worker=full_id(CURRENT))
    await shop.run(job)
    early_state = full_id(REPLACED) in shop.host.image_store
    shop.advance(GRACE_SECONDS)

    result = await shop.run(job)

    [removed] = shop.rows("image_removed")
    assert early_state
    assert counts(result)["pruned"] == 1
    assert (removed["actor_subsystem"], removed["rule_id"]) == ("container-updater", rule_id)
    assert (removed["detail"]["replaced_target_type"], removed["detail"]["replaced_target"]) == (
        "stack",
        SHOP,
    )
    assert full_id(REPLACED) not in shop.host.image_store
    assert await records(shop) == []


async def test_a_stack_with_a_container_that_is_not_running_defers_the_removal(
    shop: World, pruning_shop: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, job = pruning_shop
    redeploy_leaves(shop, monkeypatch, web=full_id(CURRENT), worker=full_id(CURRENT))
    await shop.run(job)
    project_boxes(shop)[0].state = "exited"
    shop.advance(GRACE_SECONDS)

    result = await shop.run(job)

    assert shop.rows("image_removed") == []
    assert full_id(REPLACED) in shop.host.image_store
    assert len(await records(shop)) == 1
    assert "pruned" not in counts(result)


async def test_a_rule_that_does_not_prune_records_nothing_when_it_redeploys(shop: World) -> None:
    _, job = await shop.rule(stack_selector(), {"action": "update", "verify_seconds": 0})

    await shop.run(job)

    assert await records(shop) == []
