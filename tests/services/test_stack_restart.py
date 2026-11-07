"""A stack restart made on behalf of an automation rule: stop, then start, the running services."""

from pathlib import Path
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound
from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_deploy import NotDeployed, StackRejected
from app.services.stack_views import NotRunning
from app.subsystems.base import RunContext
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import RULE_NAME, STACK_RESTARTER, authority_context, event_rows
from tests.stack_support import Rig, project_box, read_records, seed_stack_row

SHOP = "shop"
STACK_KEY = ObjectKey(ENV_A, "stack", SHOP)
SHORT_WAIT = 0.05


def stack_file(record: Path, *, start: str = "ok") -> str:
    return (
        "services:\n  db:\n    image: nginx:1\n  web:\n    image: nginx:1\n"
        f"x-fake:\n  record: {record}\n  start: {start}\n"
    )


def calls(record: Path) -> list[list[str]]:
    found = []
    for call in read_records(record):
        argv = call["argv"]
        start = argv.index("--env-file") + 2
        if argv[start] == "--profile":
            start += 2
        if argv[start] in {"stop", "start", "restart"}:
            found.append(argv[start:])
    return found


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "compose-record.jsonl"


async def deploy(rig: Rig, record: Path, *, start: str = "ok", state: str = "running") -> None:
    await seed_stack_row(rig, ENV_A, SHOP, stack_file(record, start=start))
    project_box(rig.host, 1, SHOP, "db", state=state)
    project_box(rig.host, 2, SHOP, "web", state=state)


async def authority(rig: Rig, **options: Any) -> tuple[RunContext, AutomationAuthority]:
    context = await authority_context(
        rig.db,
        rig.clock,
        rig.engine.locks,
        environment_id=ENV_A,
        subsystem=STACK_RESTARTER,
        **options,
    )
    return context, context.authority()


async def test_a_restart_stops_then_starts_the_running_services_and_records_the_rule(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    context, acting = await authority(stack_rig)

    outcome = await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert calls(record) == [["stop", "db", "web"], ["start", "db", "web"]]
    assert (outcome.operation, outcome.status, outcome.services_running) == (
        "restart",
        "active",
        2,
    )
    [row] = await event_rows(stack_rig.db, "stack_restarted")
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        STACK_RESTARTER,
    )
    assert (row["rule_id"], row["job_run_id"]) == (
        context.actor.rule_id,
        context.actor.job_run_id,
    )
    assert row["detail"]["method"] == "stop_start"
    assert row["detail"]["services"] == "db,web"
    assert (row["detail"]["stop_exit_code"], row["detail"]["exit_code"]) == (0, 0)
    assert row["detail"]["rule_name"] == RULE_NAME
    assert "phase" not in row["detail"]
    shown = await stack_rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)
    assert (shown.row.last_operation, shown.row.last_outcome) == ("start", "success")
    assert stack_rig.engine.locks.holder_of(STACK_KEY) is None


async def test_a_service_that_is_not_running_is_left_out_unless_the_caller_names_it(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    next(b for b in stack_rig.host.boxes.values() if b.name.endswith("-1")).state = "exited"
    _, acting = await authority(stack_rig)

    await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)
    await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP, also_start=["db"])

    assert calls(record) == [
        ["stop", "web"],
        ["start", "web"],
        ["stop", "db", "web"],
        ["start", "db", "web"],
    ]


async def test_a_failed_start_names_the_phase_and_the_services_left_unstarted(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record, start="fail")
    _, acting = await authority(stack_rig)

    with pytest.raises(Exception, match=r".+"):
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    [row] = await event_rows(stack_rig.db, "stack_restarted")
    detail = row["detail"]
    assert row["result"] == "failure"
    assert (detail["phase"], detail["method"]) == ("start", "stop_start")
    assert (detail["stop_exit_code"], detail["exit_code"]) == (0, 1)
    assert detail["unstarted"] == "db,web"
    shown = await stack_rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)
    assert (shown.row.last_operation, shown.row.last_outcome) == ("start", "failure")


async def test_a_stack_with_nothing_running_is_not_restarted_and_the_refusal_is_recorded(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record, state="exited")
    _, acting = await authority(stack_rig)

    with pytest.raises(NotRunning):
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert calls(record) == []
    [row] = await event_rows(stack_rig.db, "stack_restarted")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "not_running")


async def test_a_stack_with_no_containers_is_not_deployed(stack_rig: Rig, record: Path) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, stack_file(record))
    _, acting = await authority(stack_rig)

    with pytest.raises(NotDeployed):
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert calls(record) == []


@pytest.mark.parametrize("name", ["missing", "Shop!"])
async def test_a_stack_that_does_not_exist_is_not_found(
    stack_rig: Rig, record: Path, name: str
) -> None:
    await deploy(stack_rig, record)
    _, acting = await authority(stack_rig)

    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.restart(acting, ENV_A, name)


async def test_a_swarm_stack_is_not_restarted(stack_rig: Rig, record: Path) -> None:
    await deploy(stack_rig, record)
    await stack_rig.db.execute("UPDATE stack SET kind = 'swarm'")
    _, acting = await authority(stack_rig)

    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert calls(record) == []


async def test_a_member_the_owner_may_not_change_refuses_the_restart_before_compose_runs(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    web = next(b.id for b in stack_rig.host.boxes.values() if b.name.endswith("-2"))
    _, acting = await authority(stack_rig, allow=lambda key: key.object_id != web)

    with pytest.raises(StackRejected) as refused:
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert refused.value.reason == "restricted"
    assert calls(record) == []
    assert [r["detail"]["reason"] for r in await event_rows(stack_rig.db, "stack_restarted")] == [
        "restricted"
    ]


async def test_a_stack_a_member_or_a_recent_user_action_holds_back_writes_no_row(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    suppressions = SuppressionRegistry(stack_rig.clock)
    _, acting = await authority(stack_rig, lock_wait=SHORT_WAIT, suppressions=suppressions)
    locks = stack_rig.engine.locks
    member = next(iter(stack_rig.host.boxes.values())).id
    user = LockHolder(UserActor.of(ADMIN), ADMIN.username, "redeploy")

    async with locks.hold(STACK_KEY, user):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)
    async with locks.hold(ObjectKey(ENV_A, "container", member), user):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)
    suppressions.note(STACK_KEY)
    with pytest.raises(RunSkipped):
        await stack_rig.fresh().actions.restart(acting, ENV_A, SHOP)

    assert calls(record) == []
    assert await event_rows(stack_rig.db, "stack_restarted") == []
    assert locks.holder_of(STACK_KEY) is None
