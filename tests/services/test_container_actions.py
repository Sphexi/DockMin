"""Lifecycle actions: audit rows, locks, error mapping, cancellation, removal and batches."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.scheduler.suppression import SuppressionRegistry
from app.services.activity import UserActor
from app.services.container_actions import (
    CHANGED_SINCE_REVIEW,
    MAX_BATCH,
    ContainerGone,
    ContainerStateConflict,
    EngineRefused,
    WaitBudget,
)
from app.services.containers import CONTAINER_ACTIONS, ContainerAction
from app.services.network_joiners import MAX_JOINER_CHAIN
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control
from tests.access_support import container_key as key_of
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_ID, STANDARD_USERNAME, FakeClock
from tests.container_support import ABSENT, CACHE, JOB, STACK, STACK_LABEL, WARM, WEB, stock
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine
from tests.docker_engine.fake_engine import Refuse, Reply, Stall
from tests.environment_support import activity_rows
from tests.services.conftest import ContainerRig

SECOND_JOB = container_id(0xB1)
OWNER = container_id(0xC1)
BY_ID = container_id(0xC2)
BY_NAME = container_id(0xC3)
STOPPED = container_id(0xC4)
READ_ONLY = container_id(0xC5)
UNSEEN = container_id(0xC6)
HEALTHY_AFTER_POLLS = 2
GRANDCHILD = container_id(0xC7)
LATE = container_id(0xC8)
PLAIN_OWNER = container_id(0xE0)
JOINER_BATCH_WAIT_SECONDS = 90
JOINER_POLL = 2
OVER_THE_CAP = {"Content-Length": "1" + "0" * 12}
DOCKER_REFUSED = "Docker refused the action."
NOT_RUNNING = "The container is not running."
RUNNING_NEEDS_STOP = "The container is running. Stop it first, or choose to kill it first."


@pytest.fixture
def stocked(rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
    stock(host)
    return rig


@pytest.fixture
async def shared(stocked: ContainerRig) -> ContainerRig:
    """WEB is writable by the standard user, JOB readable only, CACHE not granted."""
    await control(stocked.access, key_of(0xA1, ENV_A), public=True, level="write")
    await control(stocked.access, key_of(0xA5, ENV_A), public=True, level="read")
    return stocked


def hold_of(who: Principal, action: str = "stop") -> LockHolder:
    return LockHolder(UserActor(who.user_id), who.username, action)


def key(container: str, environment_id: int = ENV_A) -> ObjectKey:
    return ObjectKey(environment_id, "container", container)


async def container_rows(rig: ContainerRig) -> list[dict[str, Any]]:
    rows = await activity_rows(rig.db)
    return [row for row in rows if row["action"].startswith("container_")]


async def act(
    rig: ContainerRig,
    action: ContainerAction,
    container: str = WEB,
    who: Principal = ADMIN,
    environment_id: int = ENV_A,
    **options: Any,
) -> Any:
    if action == "remove":
        options.setdefault("expected_name", "")
    return await rig.services.actions.perform(who, environment_id, container, action, **options)


@pytest.mark.parametrize(
    ("action", "container", "event", "name", "extra"),
    [
        ("start", JOB, "container_started", "job", {"changed": True}),
        ("stop", WEB, "container_stopped", "web", {"changed": True}),
        ("restart", WEB, "container_restarted", "web", {}),
        ("kill", WEB, "container_killed", "web", {}),
        ("pause", WEB, "container_paused", "web", {}),
        ("unpause", CACHE, "container_unpaused", "cache", {}),
        ("remove", JOB, "container_removed", "job", {"force": False, "volumes": False}),
    ],
)
async def test_every_action_records_one_success_row_with_the_user_the_target_and_the_environment(
    stocked: ContainerRig,
    action: ContainerAction,
    container: str,
    event: str,
    name: str,
    extra: dict[str, bool],
) -> None:
    await act(stocked, action, container, expected_name=name)

    assert await container_rows(stocked) == [
        {
            "action": event,
            "result": "success",
            "actor_type": "user",
            "actor_user_id": ADMIN_ID,
            "target_type": "container",
            "target_id": container,
            "environment_id": ENV_A,
            "detail": {"name": name, **extra},
        }
    ]


async def test_an_action_that_changes_nothing_succeeds_as_unchanged_and_still_records_the_attempt(
    stocked: ContainerRig,
) -> None:
    outcome = await act(stocked, "start", WEB)

    assert outcome.result == "unchanged"
    rows = await container_rows(stocked)
    assert [(r["result"], r["detail"]["changed"]) for r in rows] == [("success", False)]


@pytest.mark.parametrize(
    ("setup", "action", "container", "error", "reason", "message"),
    [
        pytest.param(
            lambda rig, host: host.fail("stop", 404),
            "stop",
            WEB,
            ContainerGone,
            "not_found",
            "The container no longer exists.",
            id="gone",
        ),
        pytest.param(
            lambda rig, host: None,
            "pause",
            JOB,
            ContainerStateConflict,
            "conflict",
            NOT_RUNNING,
            id="conflict",
        ),
        pytest.param(
            lambda rig, host: host.fail("stop", 500),
            "stop",
            WEB,
            EngineRefused,
            "engine_error",
            DOCKER_REFUSED,
            id="engine-refused",
        ),
        pytest.param(
            lambda rig, host: host.route("POST", f"/containers/{WEB}/stop", Refuse()),
            "stop",
            WEB,
            EnvironmentUnavailable,
            "unreachable",
            "The Docker host could not be reached.",
            id="unreachable",
        ),
        pytest.param(
            lambda rig, host: host.route("POST", f"/containers/{WEB}/stop", Stall()),
            "stop",
            WEB,
            EnvironmentUnavailable,
            "outcome_unknown",
            "The Docker host did not answer in time. The action may still complete.",
            id="outcome-unknown",
        ),
    ],
)
async def test_each_failure_kind_records_one_failure_row_with_a_product_message_and_frees_the_lock(
    stocked: ContainerRig,
    host: ContainerEngine,
    setup: Any,
    action: ContainerAction,
    container: str,
    error: type[Exception],
    reason: str,
    message: str,
) -> None:
    setup(stocked, host)

    with pytest.raises(error):
        await act(stocked, action, container)

    rows = await container_rows(stocked)
    assert [(r["result"], r["actor_user_id"], r["environment_id"]) for r in rows] == [
        ("failure", ADMIN_ID, ENV_A)
    ]
    assert (rows[0]["detail"]["reason"], rows[0]["detail"]["message"]) == (reason, message)
    assert ENGINE_TEXT not in str(rows)
    assert len(stocked.locks) == 0


async def test_a_busy_container_records_a_failure_row_naming_the_action_and_no_username(
    stocked: ContainerRig,
) -> None:
    async with stocked.locks.hold(key(WEB), hold_of(STANDARD, "restart")):
        with pytest.raises(ObjectBusy):
            await act(stocked, "stop", WEB)

    rows = await container_rows(stocked)
    assert rows[0]["result"] == "failure"
    assert rows[0]["detail"]["reason"] == "object_busy"
    assert rows[0]["detail"]["holder_action"] == "restart"
    assert STANDARD_USERNAME not in str(rows)


@pytest.mark.parametrize(
    ("viewer", "holder", "name", "held_by_you"),
    [
        (ADMIN, ADMIN, ADMIN_USERNAME, True),
        (ADMIN, STANDARD, STANDARD_USERNAME, False),
        (STANDARD, STANDARD, "you", True),
        (STANDARD, ADMIN, "another user", False),
    ],
    ids=["admin-sees-self", "admin-sees-user", "user-sees-self", "user-sees-other"],
)
async def test_the_busy_view_names_the_holder_by_the_viewers_standing(
    stocked: ContainerRig, viewer: Principal, holder: Principal, name: str, held_by_you: bool
) -> None:
    busy = ObjectBusy(LockHolder(UserActor(holder.user_id), holder.username, "stop"))

    view = stocked.services.actions.describe_busy(busy, viewer)

    assert (view.holder, view.held_by_you, view.action) == (name, held_by_you, "stop")


async def test_two_simultaneous_stops_serialise_and_the_second_reports_the_first_as_holder(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("stop")
    first = asyncio.create_task(act(stocked, "stop", WEB))
    await gate.entered.wait()

    with pytest.raises(ObjectBusy) as second:
        await act(stocked, "stop", WEB)
    gate.release.set()
    outcome = await first

    assert second.value.holder.actor == UserActor(ADMIN_ID)
    assert second.value.holder.action == "stop"
    assert outcome.result == "done"
    assert len(host.calls("POST", f"/containers/{WEB}/stop")) == 1
    assert len(stocked.locks) == 0


async def test_an_administrators_kill_proceeds_while_a_stop_is_waiting_out_its_grace_period(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("stop")
    stopping = asyncio.create_task(act(stocked, "stop", WEB))
    await gate.entered.wait()

    killed = await act(stocked, "kill", WEB)
    gate.release.set()
    await stopping

    assert killed.result == "done"
    assert host.calls("POST", f"/containers/{WEB}/kill") != []


@pytest.mark.parametrize("holding", ["stop", "restart"])
async def test_an_administrator_kill_passes_a_holder_of_a_stop_or_restart(
    stocked: ContainerRig, holding: str
) -> None:
    async with stocked.locks.hold(key(WEB), hold_of(STANDARD, holding)):
        outcome = await act(stocked, "kill", WEB)

    assert outcome.result == "done"


async def test_an_administrator_kill_that_passes_a_holder_is_noted_as_a_user_action(
    stocked: ContainerRig,
) -> None:
    seen: list[tuple[ObjectKey, str]] = []
    async with stocked.locks.hold(key(WEB), hold_of(STANDARD, "stop")):
        stocked.locks.set_observer(lambda noted, holder: seen.append((noted, holder.action)))

        await act(stocked, "kill", WEB)

    assert seen[0] == (key(WEB), "kill")


async def test_an_administrator_kill_that_passes_a_holder_suppresses_automation_on_that_container(
    stocked: ContainerRig,
) -> None:
    suppressions = SuppressionRegistry(stocked.locks.now)
    stocked.locks.set_observer(suppressions.observe)
    async with stocked.locks.hold(key(WEB), hold_of(STANDARD, "stop")):
        await act(stocked, "kill", WEB)

    assert suppressions.last_user_action(key(WEB)) is not None


@pytest.mark.parametrize("holding", ["kill", "pause", "remove"])
async def test_a_kill_is_busy_over_any_other_holder(stocked: ContainerRig, holding: str) -> None:
    async with stocked.locks.hold(key(WEB), hold_of(STANDARD, holding)):
        with pytest.raises(ObjectBusy):
            await act(stocked, "kill", WEB)


async def test_a_standard_users_kill_is_busy_over_a_stop_holder(shared: ContainerRig) -> None:
    async with shared.locks.hold(key(WEB), hold_of(ADMIN, "stop")):
        with pytest.raises(ObjectBusy):
            await act(shared, "kill", WEB, STANDARD)


async def test_cancelling_a_stop_in_flight_lets_it_finish_and_records_it_as_interrupted(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("stop")
    request = asyncio.create_task(act(stocked, "stop", WEB))
    await gate.entered.wait()

    request.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    rows = await container_rows(stocked)
    assert [(r["action"], r["result"]) for r in rows] == [("container_stopped", "success")]
    assert rows[0]["detail"] == {"name": "web", "changed": True, "interrupted": True}
    assert host.boxes[WEB].state == "exited"
    assert len(stocked.locks) == 0


async def test_cancelling_a_call_the_daemon_then_refuses_records_a_failure_with_its_message(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("stop")
    host.fail("stop", 500)
    request = asyncio.create_task(act(stocked, "stop", WEB))
    await gate.entered.wait()

    request.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    rows = await container_rows(stocked)
    assert rows[0]["result"] == "failure"
    assert (rows[0]["detail"]["reason"], rows[0]["detail"]["message"]) == (
        "engine_error",
        DOCKER_REFUSED,
    )
    assert rows[0]["detail"]["interrupted"] is True


async def test_cancelling_before_the_call_is_dispatched_records_nothing_and_frees_the_lock(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("inspect")
    request = asyncio.create_task(act(stocked, "stop", WEB))
    await gate.entered.wait()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert await container_rows(stocked) == []
    assert host.mutations() == []
    assert len(stocked.locks) == 0


@pytest.mark.parametrize(
    ("container", "error"),
    [
        pytest.param(CACHE, ResourceNotFound, id="not-granted"),
        pytest.param(JOB, PermissionDenied, id="read-only"),
        pytest.param(ABSENT, ResourceNotFound, id="absent"),
        pytest.param("web", ResourceNotFound, id="a-name"),
        pytest.param(WEB[:12], ResourceNotFound, id="a-short-id"),
    ],
)
async def test_a_refused_standard_user_gets_the_error_and_leaves_no_row_and_no_mutation(
    shared: ContainerRig, host: ContainerEngine, container: str, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await act(shared, "stop", container, STANDARD)

    assert await container_rows(shared) == []
    assert host.mutations() == []


@pytest.mark.parametrize("action", CONTAINER_ACTIONS)
async def test_a_standard_user_cannot_run_any_action_on_a_local_socket_container(
    rig: ContainerRig, host: ContainerEngine, action: ContainerAction
) -> None:
    stock(host)
    await control(rig.access, key_of(0xA1, ENV_B), public=True, level="write")

    with pytest.raises(PermissionDenied):
        await act(rig, action, WEB, STANDARD, ENV_B, expected_name="web")

    assert await container_rows(rig) == []
    assert host.mutations() == []


async def test_a_host_failure_while_authorising_a_standard_user_writes_no_row(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    host.route("GET", f"/containers/{CACHE}/json", Refuse())

    with pytest.raises(EnvironmentUnavailable):
        await act(shared, "stop", CACHE, STANDARD)

    assert await container_rows(shared) == []


async def test_a_name_that_is_a_hidden_containers_id_neither_reaches_it_nor_is_acted_on(
    shared: ContainerRig, host: ContainerEngine
) -> None:
    alias = container_id(0xD1)
    host.add(container_id(0xC1), alias)

    with pytest.raises(ResourceNotFound):
        await act(shared, "stop", alias, STANDARD)

    assert host.mutations() == []
    assert await container_rows(shared) == []


async def test_removing_with_a_name_other_than_the_one_reviewed_removes_nothing_and_says_why(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await act(stocked, "remove", JOB, expected_name="renamed")

    assert refused.value.fields == {"name": CHANGED_SINCE_REVIEW}
    assert JOB in host.boxes
    assert host.mutations() == []
    rows = await container_rows(stocked)
    assert (rows[0]["result"], rows[0]["detail"]["reason"]) == ("failure", "name_changed")


async def test_a_removal_defaults_to_keeping_volumes_and_not_forcing(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    await act(stocked, "remove", JOB, expected_name="job")

    sent = host.calls("DELETE", f"/containers/{JOB}")[0]
    assert (sent.query["force"], sent.query["v"]) == ("false", "false")
    assert JOB not in host.boxes


async def test_removing_a_running_container_without_force_is_refused_and_with_force_succeeds(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(ContainerStateConflict) as refused:
        await act(stocked, "remove", WEB, expected_name="web")

    await act(stocked, "remove", WEB, expected_name="web", force=True, volumes=True)

    assert str(refused.value) == RUNNING_NEEDS_STOP
    rows = await container_rows(stocked)
    assert rows[-1]["detail"] == {"name": "web", "force": True, "volumes": True}
    assert WEB not in host.boxes


async def test_a_removal_releases_the_resource_control_in_the_same_unit_as_its_success_row(
    stocked: ContainerRig,
) -> None:
    await control(stocked.access, key_of(0xA5, ENV_A), public=True)
    controls = stocked.access.controls

    await act(stocked, "remove", JOB, expected_name="job")

    assert await controls.get(ADMIN, key_of(0xA5, ENV_A)) is None
    assert [r["action"] for r in await container_rows(stocked)] == ["container_removed"]


async def test_a_failed_removal_keeps_the_control(stocked: ContainerRig) -> None:
    await control(stocked.access, key_of(0xA1, ENV_A), public=True)

    with pytest.raises(ContainerStateConflict):
        await act(stocked, "remove", WEB, expected_name="web")

    assert await stocked.access.controls.get(ADMIN, key_of(0xA1, ENV_A)) is not None


async def test_a_release_that_fails_after_the_engine_removed_the_container_still_reports_success(
    stocked: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(self: ResourceControlService, key: Any) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ResourceControlService, "release", failing)

    outcome = await act(stocked, "remove", JOB, expected_name="job")

    rows = await container_rows(stocked)
    assert outcome.result == "done" and JOB not in host.boxes
    assert (rows[0]["result"], rows[0]["detail"]["control_released"]) == ("success", False)


async def test_a_batch_reports_each_item_does_not_stop_at_a_failure_and_rolls_nothing_back(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    async with stocked.locks.hold(key(WARM), hold_of(STANDARD, "restart")):
        result = await stocked.services.actions.run_batch(
            ADMIN, ENV_A, "start", [JOB, WEB, CACHE, ABSENT, WARM]
        )

    assert [(i.status, i.name) for i in result.items] == [
        ("done", "job"),
        ("unchanged", "web"),
        ("conflict", None),
        ("not_found", None),
        ("busy", None),
    ]
    assert (result.succeeded, result.failed) == (2, 3)
    assert result.items[4].holder is not None and result.items[4].holder.holder == STANDARD_USERNAME
    assert host.boxes[JOB].state == "running"


async def test_every_batch_item_is_audited_on_its_own_with_the_batch_flag(
    stocked: ContainerRig,
) -> None:
    await stocked.services.actions.run_batch(ADMIN, ENV_A, "start", [JOB, WEB, CACHE])

    rows = await container_rows(stocked)
    assert [(r["target_id"], r["result"], r["detail"]["batch"]) for r in rows] == [
        (JOB, "success", True),
        (WEB, "success", True),
        (CACHE, "failure", True),
    ]


async def test_a_batch_submitted_in_order_is_processed_in_order_with_repeats_run_once(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    result = await stocked.services.actions.run_batch(ADMIN, ENV_A, "start", [JOB, JOB, JOB])

    assert [item.container_id for item in result.items] == [JOB]
    assert len(host.calls("POST", f"/containers/{JOB}/start")) == 1


async def test_a_standard_users_batch_reports_forbidden_and_not_found_and_audits_only_what_ran(
    shared: ContainerRig,
) -> None:
    result = await shared.services.actions.run_batch(STANDARD, ENV_A, "stop", [JOB, CACHE, WEB])

    assert [i.status for i in result.items] == ["forbidden", "not_found", "done"]
    assert [r["target_id"] for r in await container_rows(shared)] == [WEB]


@pytest.mark.parametrize("count", [0, MAX_BATCH + 1])
async def test_a_selection_that_is_empty_or_over_the_cap_is_refused_before_anything_runs(
    stocked: ContainerRig, host: ContainerEngine, count: int
) -> None:
    ids = [container_id(0x100 + n) for n in range(count)]

    with pytest.raises(ValidationFailed) as refused:
        await stocked.services.actions.run_batch(ADMIN, ENV_A, "stop", ids)

    assert "containers" in refused.value.fields
    assert host.requests == []


async def test_a_selection_of_exactly_the_cap_is_accepted(stocked: ContainerRig) -> None:
    ids = [container_id(0x100 + n) for n in range(MAX_BATCH)]

    result = await stocked.services.actions.run_batch(ADMIN, ENV_A, "stop", ids)

    assert len(result.items) == MAX_BATCH


async def test_a_batch_removal_needs_the_names_reviewed_for_each_container(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add(SECOND_JOB, "second", state="exited")

    result = await stocked.services.actions.run_batch(
        ADMIN, ENV_A, "remove", [JOB, SECOND_JOB], expected_names={JOB: "job"}
    )

    assert [i.status for i in result.items] == ["done", "conflict"]
    assert result.items[1].message == CHANGED_SINCE_REVIEW
    assert (JOB in host.boxes, SECOND_JOB in host.boxes) == (False, True)


async def test_a_confirmation_names_the_target_its_host_and_whether_the_host_is_local(
    stocked: ContainerRig,
) -> None:
    remote = await stocked.services.actions.confirmation(ADMIN, ENV_A, WEB, "stop")
    local = await stocked.services.actions.confirmation(ADMIN, ENV_B, WEB, "stop")

    assert (remote.name, remote.short_id, remote.image) == ("web", WEB[:12], "nginx:1")
    assert (remote.environment_name, remote.is_local) == ("environment-a", False)
    assert (local.environment_name, local.is_local) == ("environment-b", True)


async def test_a_removal_confirmation_marks_which_mounted_volumes_are_anonymous(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.volumes.update({"data": False, "scratch": True})
    host.boxes[WEB].mounts = [
        {"Type": "volume", "Name": "data", "Destination": "/data", "RW": True},
        {"Type": "volume", "Name": "scratch", "Destination": "/tmp", "RW": True},
        {"Type": "volume", "Name": "unknown", "Destination": "/x", "RW": True},
        {"Type": "bind", "Source": "/srv", "Destination": "/srv", "RW": True},
    ]

    shown = await stocked.services.actions.confirmation(ADMIN, ENV_A, WEB, "remove")

    assert [(v.name, v.destination, v.anonymous) for v in shown.volumes] == [
        ("data", "/data", False),
        ("scratch", "/tmp", True),
        ("unknown", "/x", None),
    ]
    assert shown.volumes_hidden == 0


async def test_a_removal_confirmation_lists_twenty_volumes_and_counts_the_rest(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.boxes[WEB].mounts = [
        {"Type": "volume", "Name": f"v{n}", "Destination": f"/v{n}", "RW": True} for n in range(22)
    ]

    shown = await stocked.services.actions.confirmation(ADMIN, ENV_A, WEB, "remove")

    assert (len(shown.volumes), shown.volumes_hidden) == (20, 2)
    assert len(host.calls("GET")) <= 2 + 20 + 3


@pytest.mark.parametrize(("container", "can_force"), [(WEB, True), (JOB, False)])
async def test_force_is_offered_only_for_a_container_that_is_not_stopped(
    stocked: ContainerRig, container: str, can_force: bool
) -> None:
    shown = await stocked.services.actions.confirmation(ADMIN, ENV_A, container, "remove")

    assert shown.can_force is can_force


async def test_a_read_holder_gets_forbidden_for_a_confirmation_and_hidden_gets_not_found(
    shared: ContainerRig,
) -> None:
    with pytest.raises(PermissionDenied):
        await shared.services.actions.confirmation(STANDARD, ENV_A, JOB, "start")
    with pytest.raises(ResourceNotFound):
        await shared.services.actions.confirmation(STANDARD, ENV_A, CACHE, "start")


async def test_a_batch_confirmation_names_what_the_caller_may_change_and_counts_the_rest(
    shared: ContainerRig,
) -> None:
    shown = await shared.services.actions.batch_confirmation(
        STANDARD, ENV_A, "stop", [WEB, JOB, CACHE, ABSENT]
    )

    assert [(t.container_id, t.name, t.state) for t in shown.targets] == [(WEB, "web", "running")]
    assert shown.unavailable == 3


async def test_the_outcome_of_an_action_forgets_the_remembered_state(
    stocked: ContainerRig,
) -> None:
    before = await stocked.services.queries.row(ADMIN, ENV_A, WEB)

    await act(stocked, "stop", WEB)
    after = await stocked.services.queries.row(ADMIN, ENV_A, WEB)

    assert (before.state, after.state) == ("running", "exited")


async def test_the_standard_users_id_is_recorded_when_a_standard_user_acts(
    shared: ContainerRig,
) -> None:
    await act(shared, "stop", WEB, STANDARD)

    rows = await container_rows(shared)
    assert rows[0]["actor_user_id"] == STANDARD_ID


async def test_a_container_action_is_busy_while_its_stack_is_operated_on_whatever_the_project_case(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    cased = container_id(0xD1)
    host.add(cased, "cased", labels={STACK_LABEL: STACK.title()})

    async with stocked.locks.hold(ObjectKey(ENV_A, "stack", STACK), hold_of(STANDARD, "redeploy")):
        with pytest.raises(ObjectBusy) as plain:
            await act(stocked, "stop", WEB)
        with pytest.raises(ObjectBusy) as mixed:
            await act(stocked, "stop", cased)
    outcome = await act(stocked, "stop", WEB)

    assert plain.value.holder.action == mixed.value.holder.action == "redeploy"
    assert outcome.result == "done"
    rows = [r for r in await container_rows(stocked) if r["result"] == "failure"]
    assert [r["detail"]["reason"] for r in rows] == ["object_busy", "object_busy"]
    assert {r["detail"]["holder_action"] for r in rows} == {"redeploy"}
    assert STANDARD_USERNAME not in str(rows)
    assert len(host.calls("POST", f"/containers/{WEB}/stop")) == 1
    assert host.calls("POST", f"/containers/{cased}/stop") == []
    assert len(stocked.locks) == 0


def joining(owner: str) -> dict[str, Any]:
    return {"NetworkMode": f"container:{owner}"}


def restarted(host: ContainerEngine) -> list[str]:
    return [r.path.split("/")[-2] for r in host.calls("POST") if r.path.endswith("/restart")]


@pytest.fixture
def polls() -> list[float]:
    return []


@pytest.fixture
def joined(
    stocked: ContainerRig,
    host: ContainerEngine,
    clock: FakeClock,
    polls: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> ContainerRig:
    """One owner that turns healthy on the second poll, with joiners of each kind."""
    host.add(OWNER, "vpn", health="starting")
    host.add(BY_ID, "torrent", host_config=joining(OWNER))
    host.add(BY_NAME, "indexer", host_config=joining("vpn"))
    host.add(STOPPED, "idle", state="exited", host_config=joining(OWNER))
    host.add(READ_ONLY, "viewer", host_config=joining(OWNER))
    host.add(UNSEEN, "secret", host_config=joining(OWNER))

    async def sleep(seconds: float) -> None:
        polls.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        if len(polls) == HEALTHY_AFTER_POLLS:
            host.boxes[OWNER].health = "healthy"

    monkeypatch.setattr(stocked.services.actions, "_sleep", sleep)
    return stocked


async def test_restarting_an_owner_restarts_its_running_joiners_once_it_is_healthy(
    joined: ContainerRig, host: ContainerEngine, polls: list[float]
) -> None:
    for joiner in (READ_ONLY, UNSEEN):
        host.boxes[joiner].host_config = {"NetworkMode": "bridge"}

    outcome = await act(joined, "restart", OWNER)

    assert restarted(host) == [OWNER, BY_ID, BY_NAME]
    assert len(polls) == HEALTHY_AFTER_POLLS
    assert [(i.name, i.status) for i in outcome.joiners] == [
        ("torrent", "done"),
        ("indexer", "done"),
    ]
    rows = await container_rows(joined)
    assert [r["target_id"] for r in rows] == [OWNER, BY_ID, BY_NAME]
    assert {r["actor_user_id"] for r in rows} == {ADMIN_ID}
    assert rows[0]["detail"] == {"name": "vpn", "joiners": "torrent, indexer"}
    assert rows[1]["detail"] == {"name": "torrent", "joined": "vpn"}
    assert (rows[1]["action"], rows[1]["result"]) == ("container_restarted", "success")


async def test_a_joiner_the_user_may_not_restart_is_reported_and_left_alone(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    await control(joined.access, key_of(0xC1, ENV_A), public=True, level="write")
    await control(joined.access, key_of(0xC2, ENV_A), public=True, level="write")
    await control(joined.access, key_of(0xC5, ENV_A), public=True, level="read")

    confirmation = await joined.services.actions.confirmation(STANDARD, ENV_A, OWNER, "restart")
    outcome = await act(joined, "restart", OWNER, STANDARD)

    assert [j.name for j in confirmation.joiners] == ["torrent"]
    assert [j.name for j in confirmation.joiners_refused] == ["viewer"]
    assert confirmation.joiners_hidden == 2
    assert restarted(host) == [OWNER, BY_ID]
    refused = next(i for i in outcome.joiners if i.name == "viewer")
    assert refused.status == "forbidden"
    rows = await container_rows(joined)
    assert [r["target_id"] for r in rows] == [OWNER, READ_ONLY, BY_ID]
    assert rows[0]["detail"] == {
        "name": "vpn",
        "joiners": "torrent",
        "joiners_refused": "viewer",
        "joiners_hidden": 2,
    }
    assert (rows[1]["result"], rows[1]["detail"]["reason"]) == ("failure", "permission_denied")
    assert rows[1]["detail"]["joined"] == "vpn"
    assert "secret" not in str(rows)


async def test_a_batch_with_an_owner_and_its_joiner_restarts_each_once_owner_first(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    for joiner in (BY_NAME, STOPPED, READ_ONLY, UNSEEN):
        host.boxes[joiner].host_config = {"NetworkMode": "bridge"}

    confirmation = await joined.services.actions.batch_confirmation(
        ADMIN, ENV_A, "restart", [BY_ID, OWNER]
    )
    result = await joined.services.actions.run_batch(ADMIN, ENV_A, "restart", [BY_ID, OWNER, WEB])

    assert confirmation.joiners == ()
    assert restarted(host) == [OWNER, BY_ID, WEB]
    assert [(i.name, i.status) for i in result.items] == [
        ("torrent", "done"),
        ("vpn", "done"),
        ("web", "done"),
    ]
    assert result.items[0].message == "Restarted with vpn."
    assert [r["target_id"] for r in await container_rows(joined)] == [OWNER, BY_ID, WEB]


def bridged(host: ContainerEngine, *containers: str) -> None:
    for each in containers:
        host.boxes[each].host_config = {"NetworkMode": "bridge"}


async def test_the_chain_of_containers_sharing_a_network_is_named_and_restarted_in_order(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    bridged(host, READ_ONLY, UNSEEN, BY_NAME)
    host.add(GRANDCHILD, "sidecar", host_config=joining(BY_ID))

    confirmation = await joined.services.actions.confirmation(ADMIN, ENV_A, OWNER, "restart")
    outcome = await act(joined, "restart", OWNER)

    assert [j.name for j in confirmation.joiners] == ["torrent", "sidecar"]
    assert restarted(host) == [OWNER, BY_ID, GRANDCHILD]
    assert [(i.name, i.status) for i in outcome.joiners] == [
        ("torrent", "done"),
        ("sidecar", "done"),
    ]
    rows = {r["target_id"]: r for r in await container_rows(joined)}
    assert rows[GRANDCHILD]["detail"] == {"name": "sidecar", "joined": "torrent"}


async def test_a_joiner_that_appeared_after_the_confirmation_is_reported_and_left_alone(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    bridged(host, READ_ONLY, UNSEEN, BY_NAME)
    confirmation = await joined.services.actions.confirmation(ADMIN, ENV_A, OWNER, "restart")
    host.add(LATE, "late", host_config=joining(OWNER))

    outcome = await act(
        joined, "restart", OWNER, joiners=[each.container_id for each in confirmation.joiners]
    )

    assert restarted(host) == [OWNER, BY_ID]
    late = next(item for item in outcome.joiners if item.name == "late")
    assert (late.status, late.reason) == ("conflict", "not_confirmed")
    failed = [r for r in await container_rows(joined) if r["result"] == "failure"]
    assert [(r["target_id"], r["detail"]["reason"]) for r in failed] == [(LATE, "not_confirmed")]


async def test_a_confirmed_batch_restarts_only_the_joiners_it_named(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    bridged(host, READ_ONLY, UNSEEN, BY_NAME)
    confirmation = await joined.services.actions.batch_confirmation(
        ADMIN, ENV_A, "restart", [OWNER, WEB]
    )
    host.add(LATE, "late", host_config=joining(OWNER))

    result = await joined.services.actions.run_batch(
        ADMIN,
        ENV_A,
        "restart",
        [OWNER, WEB],
        joiners=[each.container_id for each in confirmation.joiners],
    )

    assert restarted(host) == [OWNER, BY_ID, WEB]
    assert {(i.name, i.status) for i in result.joiners} == {
        ("torrent", "done"),
        ("late", "conflict"),
    }


async def test_a_chain_longer_than_the_limit_restarts_the_limit_and_counts_the_rest(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add(PLAIN_OWNER, "hub")
    extra = 2
    for number in range(MAX_JOINER_CHAIN + extra):
        host.add(container_id(0x1000 + number), f"j{number:03d}", host_config=joining(PLAIN_OWNER))

    confirmation = await stocked.services.actions.confirmation(ADMIN, ENV_A, PLAIN_OWNER, "restart")
    outcome = await act(stocked, "restart", PLAIN_OWNER)

    assert len(confirmation.joiners) == MAX_JOINER_CHAIN
    assert confirmation.joiners_omitted == extra
    assert len(outcome.joiners) == MAX_JOINER_CHAIN
    assert outcome.joiners_omitted == extra
    assert len(restarted(host)) == MAX_JOINER_CHAIN + 1
    [row] = [r for r in await container_rows(stocked) if r["target_id"] == PLAIN_OWNER]
    assert row["detail"]["joiners_omitted"] == extra


async def test_a_joiner_listing_that_fails_restarts_the_owner_alone_and_says_so(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    host.route("GET", "/containers/json", Refuse())

    outcome = await act(joined, "restart", OWNER)

    assert restarted(host) == [OWNER]
    assert (outcome.result, outcome.joiners, outcome.joiners_unlisted) == ("done", (), True)
    [row] = await container_rows(joined)
    assert (row["result"], row["detail"]["joiners_unlisted"]) == ("success", True)


async def test_a_joiner_too_large_to_read_is_unnamed_for_a_standard_user_and_named_for_an_admin(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    bridged(host, BY_ID, BY_NAME, STOPPED, READ_ONLY)
    await control(joined.access, key_of(0xC1, ENV_A), public=True, level="write")
    host.route("GET", f"/containers/{UNSEEN}/json", Reply(200, b"{}", OVER_THE_CAP))

    standard = await joined.services.actions.confirmation(STANDARD, ENV_A, OWNER, "restart")
    admin = await joined.services.actions.confirmation(ADMIN, ENV_A, OWNER, "restart")

    assert (standard.joiners_hidden, standard.joiners_refused) == (1, ())
    assert (admin.joiners_hidden, admin.joiners_refused) == (0, ())
    assert [j.name for j in admin.joiners] == ["secret"]


async def test_a_batch_reads_the_hosts_containers_once(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    bridged(host, READ_ONLY, UNSEEN, BY_NAME)

    await joined.services.actions.run_batch(ADMIN, ENV_A, "restart", [OWNER, WEB, JOB])

    assert len(host.calls("GET", "/containers/json")) == 1


async def test_a_batch_shares_one_wait_across_its_owners(
    joined: ContainerRig, host: ContainerEngine, polls: list[float]
) -> None:
    bridged(host, READ_ONLY, UNSEEN, BY_NAME)
    second = container_id(0xC9)
    host.add(second, "second-vpn", health="starting")
    host.add(container_id(0xCA), "second-torrent", host_config=joining(second))

    result = await joined.services.actions.run_batch(ADMIN, ENV_A, "restart", [OWNER, second])

    assert [(i.name, i.status) for i in result.joiners] == [
        ("torrent", "done"),
        ("second-torrent", "done"),
    ]
    assert HEALTHY_AFTER_POLLS < len(polls) <= JOINER_BATCH_WAIT_SECONDS / JOINER_POLL


async def client_of(rig: ContainerRig) -> Any:
    return await rig.services.actions._hosts.client(ENV_A)


@pytest.mark.parametrize("state", ["exited", "dead", "created", "restarting"])
async def test_an_owner_that_is_not_running_when_its_wait_ends_leaves_its_joiners(
    joined: ContainerRig, host: ContainerEngine, state: str
) -> None:
    host.boxes[OWNER].state = state

    refusal = await joined.services.actions.owner_ready(await client_of(joined), OWNER)

    assert refusal is not None and refusal.reason == "owner_not_running"


async def test_an_owner_that_runs_but_stays_unhealthy_lets_its_joiners_follow_after_the_wait(
    joined: ContainerRig, host: ContainerEngine, polls: list[float]
) -> None:
    async def never_healthy(seconds: float) -> None:
        polls.append(seconds)
        joined.services.actions._clock.advance(timedelta(seconds=seconds))

    joined.services.actions._sleep = never_healthy

    refusal = await joined.services.actions.owner_ready(await client_of(joined), OWNER)

    assert refusal is None
    assert sum(polls) >= 90


async def test_an_owner_that_is_gone_or_ready_is_answered_without_waiting(
    joined: ContainerRig, host: ContainerEngine, polls: list[float]
) -> None:
    client = await client_of(joined)
    host.boxes[OWNER].health = "healthy"

    ready = await joined.services.actions.owner_ready(client, OWNER)
    missing = await joined.services.actions.owner_ready(client, ABSENT)

    assert ready is None
    assert missing is not None and missing.reason == "owner_not_running"
    assert polls == []


async def test_a_spent_budget_leaves_the_joiners_of_an_owner_that_is_not_running(
    joined: ContainerRig, host: ContainerEngine
) -> None:
    actions = joined.services.actions
    host.boxes[OWNER].state = "restarting"
    spent = WaitBudget(actions._clock() - timedelta(seconds=1), actions._clock)

    refusal = await actions.owner_ready(await client_of(joined), OWNER, spent)

    assert refusal is not None and refusal.reason == "wait_expired"
