"""A stack restart made by a user: its authorisation, its binding, its audit row and its locks."""

from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected
from app.db.repositories.resource_control import ResourceControlRepository
from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_deploy import ChangedSinceReview, NotDeployed, StackRejected
from app.services.stack_views import NotRunning, containers_digest
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.auth_support import ADMIN_ID, STANDARD_ID
from tests.automation_support import STACK_RESTARTER, authority_context, event_rows
from tests.self_support import Held, home_identity
from tests.services.test_self_protection_stacks import guarded
from tests.stack_support import Rig, project_box, read_records, seed_stack_row

SHOP = "shop"
STACK_KEY = ObjectKey(ENV_A, "stack", SHOP)
WRONG_DIGEST = "0" * 64
WEB, DB, EXTRA = 1, 2, 3


def stack_file(record: Path) -> str:
    return (
        "services:\n  db:\n    image: nginx:1\n  web:\n    image: nginx:1\n"
        f"x-fake:\n  record: {record}\n"
    )


def lifecycle(record: Path) -> list[list[str]]:
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


async def deploy(
    rig: Rig, record: Path, *, owner: int | None = None, environment_id: int = ENV_A
) -> None:
    await seed_stack_row(rig, environment_id, SHOP, stack_file(record), owner_user_id=owner)
    project_box(rig.host, WEB, SHOP, "web")
    project_box(rig.host, DB, SHOP, "db")


def digest_of_project(rig: Rig) -> str:
    return containers_digest([box.id for box in rig.host.boxes.values()])


async def rows(rig: Rig) -> list[tuple[str, str | None]]:
    found = await event_rows(rig.db, "stack_restarted")
    return [(row["result"], row["detail"].get("reason")) for row in found]


async def test_a_user_restart_stops_then_starts_the_running_services_and_records_the_user(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    next(b for b in stack_rig.host.boxes.values() if b.name.endswith(f"-{DB}")).state = "exited"

    outcome = await stack_rig.fresh().actions.restart(
        ADMIN, ENV_A, SHOP, expected_containers=digest_of_project(stack_rig)
    )

    assert lifecycle(record) == [["stop", "web"], ["start", "web"]]
    assert (outcome.operation, outcome.status) == ("restart", "partial")
    [row] = await event_rows(stack_rig.db, "stack_restarted")
    assert (row["result"], row["actor_type"], row["actor_user_id"]) == ("success", "user", ADMIN_ID)
    assert row["actor_subsystem"] is None and row["rule_id"] is None
    assert row["detail"]["method"] == "stop_start" and row["detail"]["services"] == "web"
    shown = await stack_rig.fresh().queries.detail(ADMIN, ENV_A, SHOP)
    assert (shown.row.last_operation, shown.row.last_outcome) == ("start", "success")
    assert len(stack_rig.engine.locks) == 0


async def test_a_user_restart_is_refused_when_the_containers_changed_since_the_review(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    reviewed = digest_of_project(stack_rig)
    project_box(stack_rig.host, EXTRA, SHOP, "late")

    with pytest.raises(ChangedSinceReview):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP, expected_containers=reviewed)
    with pytest.raises(ChangedSinceReview):
        await stack_rig.fresh().actions.restart(
            ADMIN, ENV_A, SHOP, expected_containers=WRONG_DIGEST
        )

    assert lifecycle(record) == []
    assert await rows(stack_rig) == [("failure", "changed_since_review")] * 2


async def test_a_user_restart_without_a_binding_acts_on_whatever_runs(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)

    await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)

    assert lifecycle(record) == [["stop", "db", "web"], ["start", "db", "web"]]


async def test_a_user_restart_refuses_arguments_that_only_an_automated_run_takes(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    _, acting = await automation(stack_rig)

    with pytest.raises(ValueError):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP, also_start=["db"])
    with pytest.raises(ValueError):
        await stack_rig.fresh().actions.restart(
            acting, ENV_A, SHOP, expected_containers=WRONG_DIGEST
        )

    assert lifecycle(record) == []


async def automation(rig: Rig) -> tuple[object, AutomationAuthority]:
    context = await authority_context(
        rig.db, rig.clock, rig.engine.locks, environment_id=ENV_A, subsystem=STACK_RESTARTER
    )
    return context, context.authority()


async def test_a_user_restart_of_a_stack_with_nothing_running_is_refused_and_recorded(
    stack_rig: Rig, record: Path
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, stack_file(record))
    project_box(stack_rig.host, WEB, SHOP, "web", state="exited")

    with pytest.raises(NotRunning):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)

    assert lifecycle(record) == []
    assert await rows(stack_rig) == [("failure", "not_running")]


async def test_a_user_restart_of_a_stack_with_no_containers_is_refused_and_recorded(
    stack_rig: Rig, record: Path
) -> None:
    await seed_stack_row(stack_rig, ENV_A, SHOP, stack_file(record))

    with pytest.raises(NotDeployed):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)

    assert lifecycle(record) == []
    assert await rows(stack_rig) == [("failure", "not_deployed")]


async def test_a_swarm_stack_or_an_unknown_name_is_not_found(stack_rig: Rig, record: Path) -> None:
    await deploy(stack_rig, record)
    await stack_rig.db.execute("UPDATE stack SET kind = 'swarm'")

    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)
    with pytest.raises(ResourceNotFound):
        await stack_rig.fresh().actions.restart(ADMIN, ENV_A, "missing")

    assert lifecycle(record) == []


async def test_the_stack_dockmin_belongs_to_is_refused_by_name_and_by_a_container_it_holds(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    by_name = guarded(stack_rig, Held(home_identity(compose_project=SHOP)))
    by_container = guarded(
        stack_rig, Held(home_identity(container=next(iter(stack_rig.host.boxes))))
    )

    with pytest.raises(SelfProtected):
        await by_name.actions.restart(ADMIN, ENV_A, SHOP)
    with pytest.raises(SelfProtected):
        await by_container.actions.restart(ADMIN, ENV_A, SHOP)

    assert lifecycle(record) == []
    assert await rows(stack_rig) == [("failure", "self_protected")] * 2
    assert len(stack_rig.engine.locks) == 0


async def test_a_standard_user_restarts_their_own_stack_unless_a_container_is_not_theirs(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record, owner=STANDARD_ID)

    await stack_rig.fresh().actions.restart(STANDARD, ENV_A, SHOP)
    project_box(stack_rig.host, EXTRA, SHOP, "extra")
    extra = next(b.id for b in stack_rig.host.boxes.values() if b.name.endswith(f"-{EXTRA}"))
    async with stack_rig.database.session() as db:
        await ResourceControlRepository(db, stack_rig.clock).create(
            ENV_A,
            "container",
            extra,
            is_public=True,
            is_administrators_only=False,
            access_level="read",
            owner_user_id=None,
        )
    before = lifecycle(record)

    with pytest.raises(StackRejected) as refused:
        await stack_rig.fresh().actions.restart(STANDARD, ENV_A, SHOP)

    assert before == [["stop", "db", "web"], ["start", "db", "web"]]
    assert refused.value.reason == "restricted"
    assert lifecycle(record) == before
    assert await rows(stack_rig) == [("success", None), ("failure", "restricted")]


async def test_a_standard_user_is_refused_a_restart_on_the_local_socket(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record, owner=STANDARD_ID, environment_id=ENV_B)

    with pytest.raises(PermissionDenied):
        await stack_rig.fresh().actions.restart(STANDARD, ENV_B, SHOP)

    assert lifecycle(record) == []


async def test_a_stack_a_user_is_changing_or_a_container_of_it_that_is_locked_refuses_the_restart(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    locks = stack_rig.engine.locks
    member = next(iter(stack_rig.host.boxes.values())).id
    other = LockHolder(UserActor(STANDARD_ID), STANDARD.username, "redeploy")

    async with locks.hold(STACK_KEY, other):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)
    async with locks.hold(ObjectKey(ENV_A, "container", member), other):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)

    assert lifecycle(record) == []
    assert await rows(stack_rig) == [("failure", "object_busy")] * 2
    assert locks.holder_of(STACK_KEY) is None


async def test_a_user_restart_suppresses_the_stack_restarter_for_the_stack(
    stack_rig: Rig, record: Path
) -> None:
    await deploy(stack_rig, record)
    suppressions = SuppressionRegistry(stack_rig.clock)
    stack_rig.engine.locks.set_observer(suppressions.observe)
    context = await authority_context(
        stack_rig.db,
        stack_rig.clock,
        stack_rig.engine.locks,
        environment_id=ENV_A,
        subsystem=STACK_RESTARTER,
        suppressions=suppressions,
    )
    assert suppressions.last_user_action(STACK_KEY) is None

    await stack_rig.fresh().actions.restart(ADMIN, ENV_A, SHOP)
    noted = suppressions.last_user_action(STACK_KEY)
    before = lifecycle(record)

    with pytest.raises(RunSkipped):
        await stack_rig.fresh().actions.restart(context.authority(), ENV_A, SHOP)

    assert noted is not None
    assert lifecycle(record) == before
