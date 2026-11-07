"""Lifecycle actions: audit rows, locks, error mapping, cancellation, removal and batches."""

import asyncio
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
)
from app.services.containers import CONTAINER_ACTIONS, ContainerAction
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control
from tests.access_support import container_key as key_of
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_ID, STANDARD_USERNAME
from tests.container_support import ABSENT, CACHE, JOB, STACK, STACK_LABEL, WARM, WEB, stock
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine
from tests.docker_engine.fake_engine import Refuse, Stall
from tests.environment_support import activity_rows
from tests.services.conftest import ContainerRig

SECOND_JOB = container_id(0xB1)
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
