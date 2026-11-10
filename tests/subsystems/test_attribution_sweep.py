"""Every row the real subsystems write names the subsystem, rule, rule name and run."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.automation_support import HEALTH, STACK_RESTARTER, Subject, cid, detail_of
from tests.docker_engine.container_engine import created_id
from tests.image_support import digest, move_sets_elsewhere
from tests.stack_support import project_box, seed_stack_row
from tests.swarm_stack_support import NAME, NAMESPACE_LABEL
from tests.swarm_updater_support import (
    SERVICE,
    STACK,
    UPDATE,
    Built,
    Shop,
    after_update,
    ecr_service,
    inject_sleeper,
    interrupted,
    paused,
    still_updating,
)
from tests.updater_support import UPDATER, World, container, stack_selector

RULE = "Sweep"
SWEEP_PAUSE = 61


def assert_attributed(
    subject: Subject, subsystem: str, rule_id: int, *, since: int = 0
) -> list[dict[str, Any]]:
    rows = subject.events()[since:]
    assert rows
    assert {(row["actor_type"], row["actor_subsystem"], row["actor_user_id"]) for row in rows} == {
        ("subsystem", subsystem, None)
    }
    assert {row["rule_id"] for row in rows} == {rule_id}
    assert all(row["job_run_id"] is not None for row in rows)
    assert {detail_of(row)["rule_name"] for row in rows} == {RULE}
    return rows


async def test_the_health_checkers_success_failure_and_exhaustion_rows_are_all_attributed(
    health: Subject,
) -> None:
    for number in range(3):
        health.host.add(cid(number + 1), f"web-{number}", health="unhealthy", labels={"app": "x"})
    health.host.fail("restart", 500, skip=1, times=1)
    rule_id, job = await health.rule(
        {"kind": "labels", "labels": [["app", "x"]]}, {"max_restarts": 1}, name=RULE
    )
    await health.run(job)
    health.advance(61)
    await health.run(job)

    rows = assert_attributed(health, HEALTH, rule_id)

    assert {row["action"] for row in rows} == {
        "container_restarted",
        "container_restart_budget_exhausted",
    }
    assert {row["result"] for row in rows} == {"success", "failure"}


async def test_the_restarters_container_and_stack_rows_are_attributed(
    restarter: Subject, tmp_path: Path
) -> None:
    record = tmp_path / "record.jsonl"
    await seed_stack_row(
        restarter.rig,
        1,
        "shop",
        f"services:\n  web:\n    image: nginx:1\nx-fake:\n  record: {record}\n",
    )
    project_box(restarter.host, 1, "shop", "web")
    restarter.host.add(cid(9), "solo")
    rule_id, stack_job = await restarter.rule({"kind": "stack", "names": ["shop"]}, name=RULE)
    _, container_job = await restarter.rule(
        {"kind": "container", "names": ["solo"]}, name=RULE + "2"
    )
    await restarter.run(stack_job)
    await restarter.run(container_job)

    rows = restarter.events()

    assert {row["action"] for row in rows} == {"stack_restarted", "container_restarted"}
    assert {(row["actor_type"], row["actor_subsystem"]) for row in rows} == {
        ("subsystem", STACK_RESTARTER)
    }
    assert all(row["rule_id"] is not None and row["job_run_id"] is not None for row in rows)
    assert {detail_of(row)["rule_name"] for row in rows} == {RULE, RULE + "2"}
    assert rule_id in {row["rule_id"] for row in rows}


async def test_the_updaters_pull_recreate_rollback_and_refusal_rows_are_all_attributed(
    world: World,
) -> None:
    def crash() -> None:
        box = world.host.boxes.get(created_id(2))
        if box is not None and box.state == "running":
            box.state = "exited"
            box.exit_code = 1

    rule_id, job = await world.rule(container("web"), name=RULE)
    await world.run(job)
    world.new_image(3)
    world.host.on("inspect", crash)
    await world.run(job)
    await world.save_rule(rule_id)
    world.host.clear_faults()
    await move_sets_elsewhere(world.db)
    world.new_image(4)
    await world.run(job)

    rows = assert_attributed(world.subject, UPDATER, rule_id)

    assert {row["action"] for row in rows} == {
        "image_pulled",
        "container_recreated",
        "container_update_rolled_back",
        "image_update_refused",
    }
    assert {row["result"] for row in rows} == {"success", "failure"}


async def test_the_updaters_stack_rows_are_attributed(shop: World) -> None:
    rule_id, job = await shop.rule(stack_selector(), name=RULE)
    await shop.run(job)

    rows = assert_attributed(shop.subject, UPDATER, rule_id)

    assert {row["action"] for row in rows} == {"image_pulled", "stack_redeployed"}


async def test_the_swarm_service_updated_rolled_back_and_refused_rows_are_all_attributed(
    swarm_web: Built,
) -> None:
    after_update(swarm_web.host, paused)
    rule_id, job = await swarm_web.subject.rule(SERVICE, UPDATE, name=RULE)
    await swarm_web.subject.run(job)
    swarm_web.manifests.default = digest(3)
    swarm_web.service.pop("UpdateStatus", None)
    await move_sets_elsewhere(swarm_web.rig.db)
    swarm_web.subject.advance(SWEEP_PAUSE)
    await swarm_web.subject.run(job)

    rows = assert_attributed(swarm_web.subject, UPDATER, rule_id)

    assert {row["action"] for row in rows} == {
        "service_updated",
        "service_rolled_back",
        "image_update_refused",
    }
    assert {row["result"] for row in rows} == {"success", "failure"}


async def test_the_swarm_credential_refresh_row_is_attributed(swarm: Any, settings: Any) -> None:
    subject, _, _ = await ecr_service(swarm, settings)
    rule_id, job = await subject.rule(SERVICE, UPDATE, name=RULE)
    await subject.run(job)

    rows = assert_attributed(subject, UPDATER, rule_id)

    assert {row["action"] for row in rows} == {"service_credentials_refreshed"}


async def test_a_swarm_watch_cancelled_mid_rollout_leaves_a_row_attributed_to_the_rule(
    swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_sleeper(monkeypatch, interrupted)
    after_update(swarm_web.host, still_updating)
    rule_id, job = await swarm_web.subject.rule(SERVICE, {**UPDATE, "verify_seconds": 4}, name=RULE)

    with pytest.raises(asyncio.CancelledError):
        await swarm_web.subject.run(job)

    [row] = assert_attributed(swarm_web.subject, UPDATER, rule_id)
    assert (row["action"], row["result"], detail_of(row)["interrupted"]) == (
        "service_updated",
        "failure",
        True,
    )


async def test_the_swarm_stacks_redeployed_rows_are_all_attributed(swarm_shop: Shop) -> None:
    shop = swarm_shop
    known = len(shop.subject.events())
    rule_id, job = await shop.subject.rule(STACK, UPDATE, name=RULE)
    shop.move()
    await shop.subject.run(job)
    shop.host.add_config(f"{NAME.lower()}_old_0123456789ab", labels={NAMESPACE_LABEL: NAME})
    shop.move(digest(5))
    shop.subject.advance(SWEEP_PAUSE)
    await shop.subject.run(job)

    rows = assert_attributed(shop.subject, UPDATER, rule_id, since=known)

    assert {row["action"] for row in rows} == {"stack_redeployed"}
    assert {row["result"] for row in rows} == {"success", "failure"}


async def test_the_swarm_stacks_refusal_row_is_attributed(swarm_shop: Shop) -> None:
    shop = swarm_shop
    known = len(shop.subject.events())
    rule_id, job = await shop.subject.rule(STACK, UPDATE, name=RULE)
    shop.move()
    await move_sets_elsewhere(shop.rig.db)
    await shop.subject.run(job)

    rows = assert_attributed(shop.subject, UPDATER, rule_id, since=known)

    assert {row["action"] for row in rows} == {"image_update_refused"}
