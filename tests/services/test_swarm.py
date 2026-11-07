"""The Swarm overview, nodes, tasks and node availability changes."""

import asyncio

import pytest

import app.services.swarm as swarm_module
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.swarm import (
    AvailabilityImpact,
    NodeChanged,
    NodeChangedSinceReview,
    NodeRefused,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, PEER, STANDARD, control
from tests.docker_engine.swarm_engine import MANAGER, WORKER, plain_spec
from tests.object_support import drain_background, rows_of
from tests.services.conftest import SwarmRig
from tests.swarm_support import MISSING_ID

DRAIN = "drain"
SETTLE_SECONDS = 0.05


async def impact(
    rig: SwarmRig, availability: str = DRAIN, node: str = WORKER
) -> AvailabilityImpact:
    result = await rig.sw.queries.availability_impact(ADMIN, ENV_A, node, availability)
    rig.fresh()
    return result


async def set_availability(
    rig: SwarmRig, availability: str, version: int, current: str = "active"
) -> NodeChanged:
    return await rig.sw.nodes.set_availability(
        ADMIN,
        ENV_A,
        WORKER,
        availability=availability,
        expected_version=version,
        expected_availability=current,
    )


def spread_service(rig: SwarmRig, replicas: int = 2) -> str:
    return rig.host.add_service(plain_spec("web", replicas=replicas))


async def test_the_administrators_overview_carries_cluster_node_and_object_counts(
    swarm: SwarmRig,
) -> None:
    spread_service(swarm)
    swarm.host.add_config("c1")
    swarm.host.add_secret("s1")
    swarm.host.add_secret("s2")
    swarm.host.nodes[WORKER]["Spec"]["Availability"] = "pause"

    overview = await swarm.sw.queries.overview(ADMIN, ENV_A)

    assert overview.administrator and overview.cluster_id == "w" * 25
    assert (overview.nodes, overview.managers, overview.manager_node_id) == (2, 1, MANAGER)
    assert (overview.nodes_ready, overview.nodes_active, overview.nodes_paused) == (2, 1, 1)
    assert (overview.services, overview.tasks_running, overview.tasks_desired) == (1, 2, 2)
    assert (overview.configs, overview.secrets) == (1, 2)


async def test_a_standard_users_overview_hides_the_cluster_and_counts_only_what_they_may_see(
    swarm: SwarmRig,
) -> None:
    mine = swarm.host.add_service(plain_spec("mine"))
    swarm.host.add_service(plain_spec("theirs"))
    swarm.host.add_config("c1")
    swarm.host.add_secret("s1")
    await control(swarm.access, ResourceKey(ENV_A, "service", mine), users=(STANDARD.user_id,))
    swarm.fresh()

    overview = await swarm.sw.queries.overview(STANDARD, ENV_A)

    assert (overview.cluster_id, overview.nodes, overview.managers, overview.manager_node_id) == (
        None,
        None,
        None,
        None,
    )
    assert (overview.services, overview.configs, overview.secrets) == (1, 0, 0)
    assert overview.tasks_desired == 1


async def test_the_node_list_marks_the_manager_this_environment_connects_to(
    swarm: SwarmRig,
) -> None:
    nodes = await swarm.sw.queries.nodes(ADMIN, ENV_A)

    assert [(n.hostname, n.role, n.is_this_manager) for n in nodes.rows] == [
        ("mgr", "manager", True),
        ("wrk", "worker", False),
    ]
    assert nodes.rows[0].leader and nodes.rows[0].reachability == "reachable"


async def test_a_node_detail_lists_the_tasks_placed_on_it_by_service_name(
    swarm: SwarmRig,
) -> None:
    spread_service(swarm)

    detail = await swarm.sw.queries.node(ADMIN, ENV_A, WORKER)

    assert detail.row.hostname == "wrk" and detail.can_change
    assert [(t.service_name, t.state) for t in detail.tasks] == [("web", "running")]
    assert detail.architecture == "x86_64" and detail.os == "linux"


@pytest.mark.parametrize("environment", [ENV_A, ENV_B])
async def test_nodes_are_for_administrators_only_on_any_environment(
    swarm: SwarmRig, environment: int
) -> None:
    with pytest.raises(PermissionDenied):
        await swarm.sw.queries.nodes(STANDARD, environment)
    with pytest.raises(PermissionDenied):
        await swarm.sw.queries.node(STANDARD, environment, WORKER)
    with pytest.raises(PermissionDenied):
        await swarm.sw.queries.availability_impact(STANDARD, environment, WORKER, DRAIN)
    with pytest.raises(PermissionDenied):
        await swarm.sw.nodes.set_availability(
            STANDARD,
            environment,
            WORKER,
            availability=DRAIN,
            expected_version=1,
            expected_availability="active",
        )

    assert swarm.host.mutating() == []


@pytest.mark.parametrize("node_id", [MISSING_ID, "wrk", "A" * 25, "../x"])
async def test_a_node_that_does_not_exist_or_is_not_an_id_is_not_found(
    swarm: SwarmRig, node_id: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await swarm.sw.queries.node(ADMIN, ENV_A, node_id)


async def test_a_task_is_shown_through_its_service_and_hidden_with_it(swarm: SwarmRig) -> None:
    service_id = swarm.host.add_service(plain_spec("web"))
    [task_id] = swarm.host.service_tasks(service_id)
    swarm.host.tasks[task_id]["Status"]["Err"] = "No such image: x " + "<script>"
    await control(
        swarm.access, ResourceKey(ENV_A, "service", service_id), users=(STANDARD.user_id,)
    )
    swarm.fresh()

    admin = await swarm.sw.queries.task(ADMIN, ENV_A, task_id)
    standard = await swarm.sw.queries.task(STANDARD, ENV_A, task_id)
    with pytest.raises(ResourceNotFound) as hidden:
        await swarm.sw.queries.task(PEER, ENV_A, task_id)
    with pytest.raises(ResourceNotFound) as missing:
        await swarm.sw.queries.task(PEER, ENV_A, MISSING_ID)

    assert admin.service_name == "web" and admin.task.id == task_id
    assert (admin.can_view_node, standard.can_view_node) == (True, False)
    assert str(hidden.value) == str(missing.value)
    assert "No such image" in admin.task.error


async def test_a_task_id_that_the_engine_resolves_to_another_task_is_not_accepted(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("web", replicas=2))
    first, second = swarm.host.service_tasks(service_id)
    swarm.host.tasks[second]["ID"] = first

    with pytest.raises(ResourceNotFound):
        await swarm.sw.queries.task(ADMIN, ENV_A, second)


async def test_the_confirmation_counts_the_tasks_a_drain_reschedules_and_where_they_go(
    swarm: SwarmRig,
) -> None:
    spread_service(swarm)

    drain = await impact(swarm)

    assert (drain.hostname, drain.current, drain.requested) == ("wrk", "active", DRAIN)
    assert (drain.tasks_running, drain.other_active_nodes, drain.stays_pending) == (1, 1, False)
    assert [(s.service_name, s.tasks) for s in drain.services] == [("web", 1)]
    assert drain.version == swarm.host.nodes[WORKER]["Version"]["Index"]


async def test_draining_the_only_active_node_says_the_tasks_stay_pending(
    swarm: SwarmRig,
) -> None:
    spread_service(swarm)
    swarm.host.nodes[MANAGER]["Spec"]["Availability"] = "pause"

    drain = await swarm.sw.queries.availability_impact(ADMIN, ENV_A, WORKER, DRAIN)

    assert drain.other_active_nodes == 0 and drain.stays_pending


async def test_draining_a_node_moves_its_tasks_and_records_who_what_and_how_many(
    swarm: SwarmRig,
) -> None:
    service_id = spread_service(swarm)
    drain = await impact(swarm)

    changed = await set_availability(swarm, DRAIN, drain.version)

    [row] = await rows_of(swarm.db, "node_availability_changed")
    live = [
        t
        for t in swarm.host.service_tasks(service_id)
        if swarm.host.tasks[t]["DesiredState"] == "running"
    ]
    assert (changed.availability_before, changed.availability_after) == ("active", DRAIN)
    assert (row["target_type"], row["target_id"], row["result"]) == ("node", WORKER, "success")
    assert row["detail"] == {
        "hostname": "wrk",
        "role": "worker",
        "availability_before": "active",
        "availability_after": DRAIN,
        "tasks_running": 1,
    }
    assert {swarm.host.tasks[t]["NodeID"] for t in live} == {MANAGER}
    swarm.fresh()
    detail = await swarm.sw.queries.node(ADMIN, ENV_A, WORKER)
    assert detail.row.availability == DRAIN


async def test_restoring_a_drained_node_to_active_is_one_post_with_the_same_binding(
    swarm: SwarmRig,
) -> None:
    drain = await impact(swarm)
    await set_availability(swarm, DRAIN, drain.version)
    swarm.fresh()
    back = await impact(swarm, "active")

    await set_availability(swarm, "active", back.version, current=DRAIN)

    assert swarm.host.nodes[WORKER]["Spec"]["Availability"] == "active"
    assert len(await rows_of(swarm.db, "node_availability_changed")) == 2


async def test_setting_the_availability_a_node_already_has_sends_nothing_and_says_unchanged(
    swarm: SwarmRig,
) -> None:
    current = await impact(swarm, "active")

    changed = await set_availability(swarm, "active", current.version)

    assert changed.unchanged and swarm.host.mutating() == []


async def test_a_node_whose_version_moved_while_its_availability_stayed_is_refused(
    swarm: SwarmRig,
) -> None:
    drain = await impact(swarm)
    swarm.host.nodes[WORKER]["Version"]["Index"] += 1

    with pytest.raises(NodeChangedSinceReview):
        await set_availability(swarm, DRAIN, drain.version)

    row = (await rows_of(swarm.db, "node_availability_changed"))[-1]
    assert row["detail"]["reason"] == "changed_since_review" and swarm.host.mutating() == []


async def test_a_node_whose_availability_changed_since_the_review_is_refused(
    swarm: SwarmRig,
) -> None:
    drain = await impact(swarm)

    with pytest.raises(NodeChangedSinceReview):
        await set_availability(swarm, DRAIN, drain.version, current="pause")

    assert swarm.host.nodes[WORKER]["Spec"]["Availability"] == "active"


@pytest.mark.parametrize(
    ("moves", "expected", "reason"),
    [
        (False, NodeRefused, "engine_rejected"),
        (True, NodeChangedSinceReview, "changed_since_review"),
    ],
)
async def test_a_500_from_the_engine_is_decided_by_re_reading_the_node(
    swarm: SwarmRig, moves: bool, expected: type[Exception], reason: str
) -> None:
    drain = await impact(swarm)
    swarm.host.stale_next = True
    swarm.host.stale_moves_version = moves

    with pytest.raises(expected):
        await set_availability(swarm, DRAIN, drain.version)

    assert (await rows_of(swarm.db, "node_availability_changed"))[-1]["detail"]["reason"] == reason


async def test_an_availability_other_than_active_pause_or_drain_is_invalid(
    swarm: SwarmRig,
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await swarm.sw.nodes.set_availability(
            ADMIN,
            ENV_A,
            WORKER,
            availability="sleep",
            expected_version=1,
            expected_availability="active",
        )

    assert "availability" in raised.value.fields
    assert (await rows_of(swarm.db, "node_availability_changed"))[-1]["detail"]["reason"] == (
        "invalid"
    )


async def test_a_node_another_change_holds_is_busy(swarm: SwarmRig) -> None:
    drain = await impact(swarm)
    holder = LockHolder(UserActor(PEER.user_id), "other-admin", "availability")

    async with swarm.engine.locks.hold(ObjectKey(ENV_A, "node", WORKER), holder):
        with pytest.raises(ObjectBusy):
            await set_availability(swarm, DRAIN, drain.version)

    assert swarm.host.mutating() == []


async def test_a_change_cancelled_after_dispatch_still_writes_its_row(
    swarm: SwarmRig,
) -> None:
    drain = await impact(swarm)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(set_availability(swarm, DRAIN, drain.version))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [row] = await rows_of(swarm.db, "node_availability_changed")
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert swarm.host.nodes[WORKER]["Spec"]["Availability"] == DRAIN


async def test_a_change_still_unfinished_when_the_wait_ends_is_outcome_unknown(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swarm_module, "SETTLE_SECONDS", SETTLE_SECONDS)
    drain = await impact(swarm)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(set_availability(swarm, DRAIN, drain.version))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    swarm.host.gate.set()
    await drain_background()

    [row] = await rows_of(swarm.db, "node_availability_changed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "outcome_unknown")
