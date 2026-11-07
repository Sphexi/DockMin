"""Create and recreate with a progress feed: the steps reported, and what a cancel does."""

import asyncio
from typing import Any

import pytest

from app.services.container_provisioning import NameChanged, RecreateFailed, SubmissionRejected
from app.services.image_jobs import JobProgress
from tests.access_support import ADMIN, ENV_A
from tests.docker_engine.container_engine import ContainerEngine, created_id
from tests.environment_support import activity_rows
from tests.provisioning_support import IMAGE, OLD, create_form, seed_recreatable
from tests.services.conftest import ContainerRig

NEW = created_id(1)
PULLED_IMAGE = "ghcr.io/org/new:2"
LAYER = "a1b2c3d4e5f6"
LAYER_LINES: list[dict[str, Any]] = [
    {"status": "Pulling fs layer", "id": LAYER},
    {"status": "Downloading", "id": LAYER, "progressDetail": {"current": 5, "total": 10}},
    {"status": "Pull complete", "id": LAYER},
]
WAIT_SECONDS = 5


async def rows_of(rig: ContainerRig, action: str) -> list[dict[str, Any]]:
    return [row for row in await activity_rows(rig.db) if row["action"] == action]


def pulling_feed() -> JobProgress:
    return JobProgress(pull_only=True)


async def test_a_create_that_pulls_reports_its_layers_and_steps_and_writes_one_row(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.pull_lines = LAYER_LINES
    feed = pulling_feed()

    outcome = await rig.services.creation.create(
        ADMIN, ENV_A, create_form(image=PULLED_IMAGE), progress=feed
    )

    view = feed.snapshot()
    assert view.stages == ("pull", "pulled", "create", "start")
    assert (view.layers_total, view.layers_done, view.percent) == (1, 1, 100)
    assert host.verbs() == ["pull", "create", "start"]
    [row] = await rows_of(rig, "container_created")
    assert (row["result"], row["target_id"]) == ("success", outcome.container_id)
    assert row["detail"]["pulled"] is True


async def test_preparing_a_create_says_whether_it_pulls_and_records_nothing_unless_refused(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_image(IMAGE)
    creation = rig.services.creation

    pulls = await creation.prepare_create(ADMIN, ENV_A, create_form(image=PULLED_IMAGE))
    present = await creation.prepare_create(ADMIN, ENV_A, create_form())
    refused_before = await rows_of(rig, "container_created")
    with pytest.raises(SubmissionRejected):
        await creation.prepare_create(ADMIN, ENV_A, create_form(image=""))

    assert (pulls, present) == (True, False)
    assert refused_before == []
    assert host.mutations() == []
    [row] = await rows_of(rig, "container_created")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")


@pytest.mark.parametrize(
    ("asked", "reason"), [(True, "cancelled"), (False, "interrupted")], ids=["asked", "shutdown"]
)
async def test_a_cancel_during_a_create_pull_stops_the_pull_and_records_one_failure(
    rig: ContainerRig, host: ContainerEngine, asked: bool, reason: str
) -> None:
    gate = host.hold("pull")
    feed = pulling_feed()
    task = asyncio.ensure_future(
        rig.services.creation.create(ADMIN, ENV_A, create_form(), progress=feed)
    )
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    feed.cancel_requested = asked
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_SECONDS)

    assert host.verbs() == ["pull"]
    [row] = await rows_of(rig, "container_created")
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == ("failure", reason, None)
    assert host.boxes == {}


async def test_a_recreate_that_pulls_reports_each_step_and_a_refused_create_rolls_back(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    host.pull_lines = LAYER_LINES
    succeeded = pulling_feed()
    failed = pulling_feed()

    outcome = await rig.services.recreation.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", pull=True, progress=succeeded
    )
    host.fail("create", 400)
    with pytest.raises(RecreateFailed) as refused:
        await rig.services.recreation.recreate(
            ADMIN, ENV_A, outcome.new_id, expected_name="web", pull=True, progress=failed
        )

    assert succeeded.snapshot().stages == ("pull", "pulled", "stop", "rename", "create", "start")
    assert failed.snapshot().stages == ("pull", "pulled", "stop", "rename", "create", "rollback")
    assert refused.value.rolled_back is True
    assert (
        host.boxes[outcome.new_id].name == "web" and host.boxes[outcome.new_id].state == "running"
    )
    rows = await rows_of(rig, "container_recreated")
    assert [row["result"] for row in rows] == ["success", "failure"]
    assert rows[1]["detail"]["rolled_back"] is True


@pytest.mark.parametrize(
    ("asked", "reason"), [(True, "cancelled"), (False, "interrupted")], ids=["asked", "shutdown"]
)
async def test_a_cancel_during_a_recreate_pull_changes_nothing_and_records_one_failure(
    rig: ContainerRig, host: ContainerEngine, asked: bool, reason: str
) -> None:
    seed_recreatable(host)
    gate = host.hold("pull")
    feed = pulling_feed()
    task = asyncio.ensure_future(
        rig.services.recreation.recreate(
            ADMIN, ENV_A, OLD, expected_name="web", pull=True, progress=feed
        )
    )
    await asyncio.wait_for(gate.entered.wait(), WAIT_SECONDS)

    feed.cancel_requested = asked
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_SECONDS)

    assert host.verbs() == ["pull"]
    assert (host.boxes[OLD].name, host.boxes[OLD].state) == ("web", "running")
    [row] = await rows_of(rig, "container_recreated")
    assert (row["result"], row["detail"]["reason"]) == ("failure", reason)


async def test_preparing_a_recreate_changes_nothing_and_records_only_a_refusal(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    recreation = rig.services.recreation

    await recreation.prepare_recreate(ADMIN, ENV_A, OLD, expected_name="web")
    cleared = await rows_of(rig, "container_recreated")
    with pytest.raises(NameChanged):
        await recreation.prepare_recreate(ADMIN, ENV_A, OLD, expected_name="other")

    assert cleared == []
    assert host.mutations() == []
    [row] = await rows_of(rig, "container_recreated")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "name_changed")
