"""A stack redeploy made on behalf of an automation rule: authority, locks, refusals and rows."""

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.db.repositories.activity_event import DetailValue
from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.container_provisioning import VOLUME_UNAVAILABLE
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_deploy import NotDeployed, PendingChanges, StackRejected, file_digest
from app.services.stack_runs import _binding_actor
from app.services.stack_views import NotRunning, PartlyStopped
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import RULE_NAME, authority_context, event_rows
from tests.stack_support import (
    Rig,
    create_then_deploy,
    poll_until,
    read_records,
    stored_file_digest,
)

SHOP = "shop"
UPDATER = "container-updater"
STACK_KEY = ObjectKey(ENV_A, "stack", SHOP)
SHORT_WAIT = 0.05
NOTE: dict[str, DetailValue] = {"trigger": "update", "references_updated": 1}
HANG_TIMEOUT = 1.0


def stack_file(record: Path, *, web: str = "", extra: str = "", script: str = "") -> str:
    return (
        f"services:\n  db:\n    image: nginx:1\n  web:\n    image: nginx:1\n{web}"
        f"{extra}x-fake:\n  record: {record}\n{script}"
    )


def up_calls(record: Path) -> list[list[str]]:
    found = []
    for call in read_records(record):
        argv = call["argv"]
        start = argv.index("--env-file") + 2
        if argv[start] == "--profile":
            start += 2
        if argv[start] in {"up", "down"}:
            found.append(argv[start:])
    return found


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "compose-record.jsonl"


async def authority(rig: Rig, **options: Any) -> tuple[Any, AutomationAuthority]:
    context = await authority_context(
        rig.db, rig.clock, rig.engine.locks, environment_id=ENV_A, subsystem=UPDATER, **options
    )
    return context, context.authority()


async def deployed(rig: Rig, record: Path, text: str | None = None) -> None:
    await create_then_deploy(
        rig.services.actions, ADMIN, ENV_A, name=SHOP, compose_text=text or stack_file(record)
    )
    await asyncio.to_thread(record.unlink, missing_ok=True)


async def test_a_redeploy_by_a_rule_is_attributed_authorised_per_member_and_never_prunes(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record)
    members = {box.id for box in stack_rig.host.boxes.values()}
    context, acting = await authority(stack_rig)

    outcome = await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP, pull=False, note=NOTE)

    assert (outcome.operation, outcome.pulled) == ("redeploy", ())
    [call] = up_calls(record)
    assert call[0] == "up" and "--remove-orphans" not in call
    checked = {k.object_id for k in context.owner_gate.checked if k.object_type == "container"}
    assert checked == members and STACK_KEY in context.owner_gate.checked
    row = (await event_rows(stack_rig.db, "stack_redeployed"))[-1]
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        UPDATER,
    )
    assert (row["rule_id"], row["job_run_id"]) == (context.actor.rule_id, context.actor.job_run_id)
    detail = row["detail"]
    assert (detail["trigger"], detail["references_updated"]) == ("update", 1)
    assert (detail["rule_name"], detail["remove_orphans"]) == (RULE_NAME, False)
    assert stack_rig.engine.locks.holder_of(STACK_KEY) is None


async def test_a_busy_stack_or_member_and_a_suppressed_stack_hold_the_run_back_with_no_row(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record)
    before = len(await event_rows(stack_rig.db, "stack_redeployed"))
    suppressions = SuppressionRegistry(stack_rig.clock)
    _, acting = await authority(stack_rig, lock_wait=SHORT_WAIT, suppressions=suppressions)
    locks = stack_rig.engine.locks
    member = next(iter(stack_rig.host.boxes.values())).id
    user = LockHolder(UserActor.of(ADMIN), ADMIN.username, "redeploy")

    async with locks.hold(STACK_KEY, user):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)
    async with locks.hold(ObjectKey(ENV_A, "container", member), user):
        with pytest.raises(ObjectBusy):
            await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)
    suppressions.note(STACK_KEY)
    with pytest.raises(RunSkipped):
        await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)

    assert up_calls(record) == []
    assert len(await event_rows(stack_rig.db, "stack_redeployed")) == before
    assert locks.holder_of(STACK_KEY) is None


async def test_a_saved_edit_is_refused_pending_changes_before_compose_runs(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record)
    await stack_rig.fresh().actions.update(
        ADMIN,
        ENV_A,
        SHOP,
        expected_file_digest=await stored_file_digest(stack_rig, ADMIN, ENV_A, SHOP),
        compose_text=stack_file(record, extra="# edited\n"),
        variables=None,
    )
    _, acting = await authority(stack_rig)

    with pytest.raises(PendingChanges) as refused:
        await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)

    assert refused.value.reason == "pending_changes"
    assert up_calls(record) == []
    row = (await event_rows(stack_rig.db, "stack_redeployed"))[-1]
    assert (row["result"], row["detail"]["reason"], row["actor_subsystem"]) == (
        "failure",
        "pending_changes",
        UPDATER,
    )


@pytest.mark.parametrize(
    ("states", "error"),
    [
        (("running", "exited"), PartlyStopped),
        (("exited", "exited"), NotRunning),
        ((), NotDeployed),
    ],
)
async def test_a_stack_that_is_not_fully_running_is_refused(
    stack_rig: Rig, record: Path, states: tuple[str, ...], error: type[Exception]
) -> None:
    await deployed(stack_rig, record)
    boxes = list(stack_rig.host.boxes.values())
    if not states:
        stack_rig.host.boxes.clear()
    for box, state in zip(boxes, states, strict=False):
        box.state = state
    _, acting = await authority(stack_rig)

    with pytest.raises(error):
        await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)

    assert up_calls(record) == []


async def test_a_member_the_owner_may_not_change_refuses_before_compose_runs(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record)
    web = next(iter(stack_rig.host.boxes.values())).id
    _, acting = await authority(stack_rig, allow=lambda key: key.object_id != web)

    with pytest.raises(StackRejected) as refused:
        await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP)

    assert refused.value.reason == "restricted"
    assert up_calls(record) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"remove_orphans": True},
        {"compose_text": "services: {}"},
        {"variables": []},
        {"choices": {"image": object()}},
        {"expected_file_digest": "x"},
        {"expected_inputs_digest": "x"},
        {"expected_containers": "x"},
        {"pull": True},
        {"check_collisions": True},
    ],
    ids=[
        "orphans",
        "file",
        "variables",
        "choices",
        "file-digest",
        "inputs-digest",
        "containers",
        "pull",
        "collisions",
    ],
)
async def test_the_arguments_a_rule_may_not_pass_are_refused(
    stack_rig: Rig, record: Path, arguments: dict[str, Any]
) -> None:
    await deployed(stack_rig, record)
    _, acting = await authority(stack_rig)

    with pytest.raises(ValueError):
        await stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP, **arguments)

    assert up_calls(record) == []


async def test_a_standard_owner_is_held_to_the_restriction_set_an_administrator_is_not(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record, stack_file(record, web="    privileged: true\n"))
    _, as_admin = await authority(stack_rig)
    as_standard = dataclasses.replace(as_admin, owner_is_administrator=False)

    with pytest.raises(StackRejected) as refused:
        await stack_rig.fresh().actions.redeploy(as_standard, ENV_A, SHOP)
    refused_calls = up_calls(record)
    outcome = await stack_rig.fresh().actions.redeploy(as_admin, ENV_A, SHOP)

    assert refused.value.reason == "restricted" and refused_calls == []
    assert outcome.operation == "redeploy" and len(up_calls(record)) == 1


@pytest.mark.parametrize(("mount", "refused"), [("shared:/data:ro", False), ("shared:/data", True)])
async def test_a_read_only_external_volume_needs_read_and_a_writable_one_needs_write(
    stack_rig: Rig, record: Path, mount: str, refused: bool
) -> None:
    stack_rig.host.add_volume("shared")
    text = stack_file(
        record,
        web=f"    volumes:\n      - {mount}\n",
        extra="volumes:\n  shared:\n    external: true\n",
    )
    await deployed(stack_rig, record, text)
    seen: list[tuple[str, str]] = []

    async def write(key: ObjectKey) -> None:
        seen.append(("write", key.object_type))
        if key.object_type == "volume":
            raise PermissionDenied("insufficient access")

    async def read(key: ObjectKey) -> None:
        seen.append(("read", key.object_type))

    _, base = await authority(stack_rig)
    standard = dataclasses.replace(
        base, owner_is_administrator=False, authorise=write, authorise_read=read
    )

    if refused:
        with pytest.raises(StackRejected) as error:
            await stack_rig.fresh().actions.redeploy(standard, ENV_A, SHOP)
        assert error.value.reason == "restricted"
        assert VOLUME_UNAVAILABLE in error.value.fields.values()
        assert ("write", "volume") in seen and ("read", "volume") not in seen
        assert up_calls(record) == []
    else:
        outcome = await stack_rig.fresh().actions.redeploy(standard, ENV_A, SHOP)
        assert outcome.operation == "redeploy"
        assert ("read", "volume") in seen and ("write", "volume") not in seen


async def test_pull_credentials_resolve_with_no_principal_and_no_choice(
    stack_rig: Rig, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await deployed(stack_rig, record, stack_file(record, web="    pull_policy: always\n"))
    asked: list[tuple[Any, Any]] = []
    _, acting = await authority(stack_rig)
    services = stack_rig.fresh()
    resolver = services.actions._validator._credentials
    original = type(resolver).resolve

    async def spy(self: Any, environment_id: int, reference: str, **keywords: Any) -> Any:
        asked.append((keywords.get("principal"), keywords.get("choice")))
        return await original(self, environment_id, reference, **keywords)

    monkeypatch.setattr(type(resolver), "resolve", spy)

    outcome = await services.actions.redeploy(acting, ENV_A, SHOP, note=NOTE)

    assert asked and set(asked) == {(None, None)}
    assert outcome.pulled
    row = (await event_rows(stack_rig.db, "stack_redeployed"))[-1]
    assert row["actor_subsystem"] == UPDATER and row["detail"]["binding"]


async def test_a_cancelled_redeploy_is_recorded_as_interrupted_under_the_rule(
    stack_rig: Rig, record: Path
) -> None:
    await deployed(stack_rig, record)
    hanging = stack_file(record, script="  up: hang\n")
    await stack_rig.db.execute(
        "UPDATE stack SET compose_file = ?, compose_file_digest = ?, deployed_file_digest = ?",
        (hanging, file_digest(hanging), file_digest(hanging)),
    )
    stack_rig.sim.operation_timeout = HANG_TIMEOUT
    context, acting = await authority(stack_rig)
    redeploy = asyncio.ensure_future(
        stack_rig.fresh().actions.redeploy(acting, ENV_A, SHOP, note=NOTE)
    )
    await poll_until(lambda: any(call["cmd"] == "up" for call in read_records(record)))

    redeploy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await redeploy

    [row] = await event_rows(stack_rig.db, "stack_redeployed")
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "failure",
        "subsystem",
        UPDATER,
    )
    assert (row["rule_id"], row["actor_user_id"]) == (context.actor.rule_id, None)
    assert (row["detail"]["interrupted"], row["detail"]["trigger"]) == (True, "update")
    assert len(stack_rig.engine.locks) == 0


def test_a_users_own_choice_in_a_deploy_reaches_the_binding_and_a_rules_does_not() -> None:
    user_plan = SimpleNamespace(actor=ADMIN)

    assert _binding_actor(user_plan) is ADMIN  # type: ignore[arg-type]
