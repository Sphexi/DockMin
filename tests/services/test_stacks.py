"""Stack create, update, redeploy, start and stop, and their exclusion against container actions."""

import hashlib
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import app.compose_cli.cli as cli
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.compose_cli.binary import ComposeBinary
from app.compose_cli.parsers import ComposeVersion
from app.compose_cli.process import run
from app.db.repositories.stack import StackRepository
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import (
    ChangedSinceReview,
    ComposeNotReady,
    NotDeployed,
    StackNameInUse,
    StackRejected,
    file_digest,
)
from app.services.stack_views import unwritable_members
from app.services.stacks import sweep_orphan_directories
from tests.access_support import ADMIN, ENV_A, ENV_B, MEMBER, STANDARD, container_id, digest_of
from tests.docker_engine.container_engine import Publish, swarm_network_id
from tests.docker_engine.swarm_engine import plain_spec
from tests.stack_support import (
    SECRET_SENTINEL,
    Rig,
    activity_dump,
    create_then_deploy,
    form_variables,
    project_box,
    read_records,
    removal_binding,
    seed_stack_row,
)
from tests.support import present

SHOP = "shop"
GREETING = "hi"
SHOP_FILE = """
services:
  web:
    image: nginx:1
    ports: ["8080:80"]
    environment:
      GREETING: ${GREETING:-hello}
    volumes:
      - data:/var/lib/data
volumes:
  data: {}
"""
OLD_VERSION = ComposeVersion("v2.20.2", 2, 20, 2)
FLOOR_TEXT = "v2.32.0"
STALE_DIGEST = "0" * 64
OLD_DIRECTORY_AGE = 3600
MIN_AGE = 600.0


async def grant_write(rig: Rig, key: ResourceKey, owner: Any, grantee: Any) -> None:
    await rig.access.controls.update(
        ADMIN,
        key,
        ControlSpec(access_level="write", owner_user_id=owner.user_id, user_ids=[grantee.user_id]),
        expected_digest=await digest_of(rig.access, key),
    )


async def sub_resources(rig: Rig, environment_id: int, name: str) -> str:
    row = present(
        await rig.db.fetch_one(
            "SELECT sub_resource_ids FROM resource_control "
            "WHERE environment_id = ? AND resource_type = 'stack' AND resource_id = ?",
            (environment_id, name),
        )
    )
    return str(row["sub_resource_ids"])


async def test_a_standard_user_creates_a_controlled_stack_whose_secret_is_only_stored_encrypted(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    variables = form_variables(("GREETING", GREETING, False), ("TOKEN", SECRET_SENTINEL, True))

    report = await rig.fresh().queries.validate(
        STANDARD, ENV_A, name=SHOP, compose_text=SHOP_FILE, variables=variables
    )
    created = await rig.fresh().actions.create(
        STANDARD, ENV_A, name=SHOP, compose_text=SHOP_FILE, variables=variables
    )

    assert [service.name for service in report.services] == ["web"]
    assert (report.volumes, report.networks) == (("shop_data",), ("shop_default",))
    assert report.images == ("nginx:1",)
    assert report.to_pull == () and report.restricted == () and report.restricted_options == ()
    assert report.compose_digest == hashlib.sha256(SHOP_FILE.encode()).hexdigest()
    assert created.control_created and not created.stale_control_replaced
    control = present(
        await rig.db.fetch_one(
            "SELECT owner_user_id, access_level FROM resource_control WHERE resource_type = 'stack'"
        )
    )
    assert (control["owner_user_id"], control["access_level"]) == (STANDARD.user_id, "write")
    stored = present(
        await rig.db.fetch_one(
            "SELECT value, value_ciphertext FROM stack_variable WHERE name = 'TOKEN'"
        )
    )
    assert stored["value"] is None
    assert SECRET_SENTINEL not in stored["value_ciphertext"]
    detail = await rig.fresh().queries.detail(STANDARD, ENV_A, SHOP)
    edit = await rig.fresh().queries.edit_context(STANDARD, ENV_A, SHOP)
    assert {(v.name, v.secret, v.value) for v in detail.variables} == {
        ("GREETING", False, GREETING),
        ("TOKEN", True, None),
    }
    assert {(v.name, v.value) for v in edit.variables if v.secret} == {("TOKEN", None)}
    row = (await rig.rows("stack_created"))[0]
    assert row["result"] == "success" and row["target_id"] == SHOP
    assert row["detail"]["compose_digest"] == report.compose_digest
    assert row["detail"]["secret_variables"] == 1 and row["detail"]["control_created"] is True
    assert SECRET_SENTINEL not in await activity_dump(rig)


async def test_creating_refuses_a_name_in_use_and_replaces_a_control_left_by_a_removed_stack(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    project_box(rig.host, 1, "Other")
    await rig.access.controls.create(
        ADMIN, ResourceKey(ENV_B, "stack", "gone"), ControlSpec("read"), expected_digest=""
    )

    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.create(ADMIN, ENV_B, name=SHOP, compose_text=SHOP_FILE)
    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.create(ADMIN, ENV_B, name="other", compose_text=SHOP_FILE)
    created = await rig.fresh().actions.create(ADMIN, ENV_B, name="gone", compose_text=SHOP_FILE)

    assert created.stale_control_replaced and created.control_created
    rows = await rig.rows("stack_created")
    assert [(r["result"], r["detail"].get("reason")) for r in rows] == [
        ("failure", "name_in_use"),
        ("failure", "name_in_use"),
        ("success", None),
    ]
    assert rows[2]["detail"]["stale_control_replaced"] is True
    control = present(
        await rig.db.fetch_one(
            "SELECT owner_user_id FROM resource_control WHERE resource_id = 'gone'"
        )
    )
    assert control["owner_user_id"] == ADMIN.user_id


async def test_saving_changes_flags_the_stack_until_it_is_redeployed_and_a_no_op_writes_no_event(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_B,
        name=SHOP,
        compose_text=SHOP_FILE,
        variables=form_variables(("A", "1", False)),
    )
    deployed = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)

    unchanged = await rig.fresh().actions.update(
        ADMIN, ENV_B, SHOP, expected_file_digest=deployed.compose_file_digest
    )
    rig.clock.advance(timedelta(seconds=5))
    variables_only = await rig.fresh().actions.update(
        ADMIN,
        ENV_B,
        SHOP,
        expected_file_digest=deployed.compose_file_digest,
        variables=form_variables(("A", "2", False), ("B", "x", True)),
    )
    after_variables = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)
    rig.clock.advance(timedelta(seconds=5))
    edited_text = SHOP_FILE.replace("8080", "9090")
    file_only = await rig.fresh().actions.update(
        ADMIN,
        ENV_B,
        SHOP,
        expected_file_digest=deployed.compose_file_digest,
        compose_text=edited_text,
    )
    after_file = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)
    await rig.fresh().actions.redeploy(ADMIN, ENV_B, SHOP)
    redeployed = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)

    assert not deployed.row.changed_since_deploy
    assert not unchanged.changed
    assert (variables_only.file_changed, variables_only.variables_changed) == (False, 2)
    assert after_variables.row.changed_since_deploy
    assert after_variables.compose_file_digest == deployed.compose_file_digest
    assert (file_only.file_changed, file_only.variables_changed) == (True, 0)
    assert after_file.compose_file_digest == hashlib.sha256(edited_text.encode()).hexdigest()
    assert after_file.deployed_file_digest == deployed.compose_file_digest
    assert not redeployed.row.changed_since_deploy
    assert redeployed.deployed_file_digest == after_file.compose_file_digest
    updates = await rig.rows("stack_updated")
    assert [r["detail"]["file_changed"] for r in updates] == [False, True]
    assert updates[1]["detail"]["compose_digest"] == after_file.compose_file_digest


async def test_changed_since_deploy_follows_the_inputs_not_the_clock(stack_rig: Rig) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_B,
        name=SHOP,
        compose_text=SHOP_FILE,
        variables=form_variables(("A", "1", False), ("S", "one", True)),
    )

    async def changed() -> bool:
        return (await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)).row.changed_since_deploy

    async def save(*variables: tuple[str, str | None, bool]) -> None:
        digest = (await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)).compose_file_digest
        await rig.fresh().actions.update(
            ADMIN, ENV_B, SHOP, expected_file_digest=digest, variables=form_variables(*variables)
        )

    after_deploy = await changed()
    await save(("A", "2", False), ("S", None, True))
    after_plain_edit = await changed()
    await save(("A", "1", False), ("S", None, True))
    after_plain_set_back = await changed()
    await save(("A", "1", False), ("S", "one", True))
    after_secret_given = await changed()

    assert not after_deploy
    assert after_plain_edit
    assert not after_plain_set_back
    assert after_secret_given


async def test_a_redeploy_bound_to_stale_inputs_is_refused_and_nothing_runs(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.fresh().actions,
        ADMIN,
        ENV_B,
        name=SHOP,
        compose_text=SHOP_FILE,
        variables=form_variables(("A", "1", False)),
    )
    review = await rig.fresh().queries.redeploy_confirmation(ADMIN, ENV_B, SHOP)
    digest = (await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)).compose_file_digest
    await rig.fresh().actions.update(
        ADMIN,
        ENV_B,
        SHOP,
        expected_file_digest=digest,
        variables=form_variables(("A", "2", False)),
    )
    mutations = len(rig.host.mutations())

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.redeploy(
            ADMIN,
            ENV_B,
            SHOP,
            expected_file_digest=review.expected_file_digest,
            expected_inputs_digest=review.expected_inputs_digest,
        )

    assert review.expected_inputs_digest
    assert len(rig.host.mutations()) == mutations


async def test_updating_a_secret_that_has_no_stored_value_is_refused_and_changes_nothing(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)

    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.update(
            ADMIN,
            ENV_B,
            SHOP,
            expected_file_digest=file_digest(SHOP_FILE),
            variables=form_variables(("TOKEN", None, True)),
        )

    assert list(refused.value.fields) == ["variable-0-value"]
    assert await rig.db.fetch_one("SELECT 1 FROM stack_variable") is None


async def test_redeploy_is_bound_to_the_reviewed_file_and_containers_and_keeps_grantee_visibility(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    key = ResourceKey(ENV_A, "stack", SHOP)
    await create_then_deploy(
        rig.fresh().actions,
        STANDARD,
        ENV_A,
        name=SHOP,
        compose_text=SHOP_FILE,
        variables=form_variables(("TOKEN", SECRET_SENTINEL, True)),
    )
    await grant_write(rig, key, STANDARD, MEMBER)
    before = await rig.fresh().queries.detail(MEMBER, ENV_A, SHOP)
    ids_before = {c.id for s in before.services for c in s.containers}
    review = await rig.fresh().queries.redeploy_confirmation(STANDARD, ENV_A, SHOP)

    outcome = await rig.fresh().actions.redeploy(
        STANDARD,
        ENV_A,
        SHOP,
        expected_file_digest=review.expected_file_digest,
        expected_containers=review.expected_containers,
        pull=True,
    )
    mutations = len(rig.host.mutations())
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.redeploy(
            STANDARD, ENV_A, SHOP, expected_containers=review.expected_containers
        )
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.redeploy(STANDARD, ENV_A, SHOP, expected_file_digest=STALE_DIGEST)

    after = await rig.fresh().queries.detail(MEMBER, ENV_A, SHOP)
    ids_after = {c.id for s in after.services for c in s.containers}
    assert len(ids_before) == 1 and len(ids_after) == 1 and ids_after != ids_before
    assert outcome.pulled == ("nginx:1",) and outcome.services_running == 1
    assert len(rig.host.mutations()) == mutations
    owner = present(
        await rig.db.fetch_one(
            "SELECT owner_user_id FROM resource_control WHERE resource_type = 'stack'"
        )
    )
    assert owner["owner_user_id"] == STANDARD.user_id
    cached = await sub_resources(rig, ENV_A, SHOP)
    assert f"container:{next(iter(ids_after))}" in cached
    assert next(iter(ids_before)) not in cached
    rows = await rig.rows("stack_redeployed")
    assert [r["result"] for r in rows] == ["success", "failure", "failure"]
    assert [r["detail"].get("reason") for r in rows[1:]] == ["changed_since_review"] * 2
    assert len(rows[0]["detail"]["compose_digest"]) == 64
    assert len(rows[0]["detail"]["resolved_digest"]) == 64
    assert SECRET_SENTINEL not in await activity_dump(rig)


async def test_a_checked_redeploy_passes_for_its_own_containers_and_refuses_an_outside_holder(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    intruder = "intruder"
    text = (
        "services:\n  web:\n    image: nginx:1\n    container_name: fixed-web\n"
        "    ports: ['8080:80']\n"
    )
    await create_then_deploy(rig.fresh().actions, ADMIN, ENV_A, name=SHOP, compose_text=text)

    def own_container_holds_the_name_and_port() -> None:
        (box,) = rig.host.boxes.values()
        box.name = "fixed-web"
        box.publishes = [Publish(8080)]

    own_container_holds_the_name_and_port()
    passed = await rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP, check_collisions=True)
    own_container_holds_the_name_and_port()
    rig.host.add(container_id(1), intruder, publishes=[Publish(8080)])
    mutations = len(rig.host.mutations())
    with pytest.raises(StackRejected) as refused:
        await rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP, check_collisions=True)

    assert passed.operation == "redeploy"
    assert refused.value.reason == "port_in_use"
    assert len(rig.host.mutations()) == mutations
    rows = await rig.rows("stack_redeployed")
    assert [r["result"] for r in rows] == ["success", "failure"]
    assert rows[1]["detail"]["reason"] == "port_in_use"
    assert intruder not in await activity_dump(rig)


async def test_start_before_any_deploy_is_refused_and_stop_and_start_follow_the_containers(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await rig.fresh().actions.create(ADMIN, ENV_B, name=SHOP, compose_text=SHOP_FILE)

    with pytest.raises(NotDeployed):
        await rig.fresh().actions.start(ADMIN, ENV_B, SHOP)
    deployed = await rig.fresh().actions.deploy(ADMIN, ENV_B, SHOP)
    stopped = await rig.fresh().actions.stop(ADMIN, ENV_B, SHOP)
    stopped_detail = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)
    started = await rig.fresh().actions.start(ADMIN, ENV_B, SHOP)
    started_detail = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)

    assert deployed.status == "active"
    assert (stopped.status, stopped.services_running) == ("inactive", 0)
    assert (started.status, started.services_running) == ("active", 1)
    assert stopped_detail.row.last_operation == "stop"
    assert (started_detail.row.last_operation, started_detail.row.last_outcome) == (
        "start",
        "success",
    )
    refused = (await rig.rows("stack_started"))[0]
    assert (refused["result"], refused["detail"]["reason"]) == ("failure", "not_deployed")
    assert [r["result"] for r in await rig.rows("stack_stopped")] == ["success"]
    assert "container:" in await sub_resources(rig, ENV_B, SHOP)


async def test_a_refusal_given_before_an_operation_starts_leaves_that_operations_failure_row(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    holder = LockHolder(UserActor(ADMIN.user_id), ADMIN.username, "stop")

    async def refused(operation: Any, error: Exception) -> None:
        with pytest.raises(type(error)):
            await rig.fresh().actions.record_refusal(
                ADMIN, ENV_B, SHOP, operation=operation, error=error
            )

    await refused("redeploy", ChangedSinceReview())
    await refused("remove", unwritable_members())
    await refused("start", ObjectBusy(holder))
    await refused("stop", ChangedSinceReview())

    rows = [
        row
        for row in await rig.rows()
        if row["action"] in {"stack_redeployed", "stack_removed", "stack_started", "stack_stopped"}
    ]
    assert [(r["action"], r["result"], r["detail"]["reason"]) for r in rows] == [
        ("stack_redeployed", "failure", "changed_since_review"),
        ("stack_removed", "failure", "restricted"),
        ("stack_started", "failure", "object_busy"),
        ("stack_stopped", "failure", "changed_since_review"),
    ]
    assert {r["target_id"] for r in rows} == {SHOP} and {r["environment_id"] for r in rows} == {
        ENV_B
    }
    assert rows[2]["detail"]["holder_action"] == "stop"


async def test_a_refusal_recorded_by_a_caller_who_can_only_read_leaves_no_row(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE, owner_user_id=ADMIN.user_id)
    await grant_write(rig, ResourceKey(ENV_B, "stack", SHOP), ADMIN, STANDARD)

    with pytest.raises(PermissionDenied):
        await rig.fresh().actions.record_refusal(
            STANDARD, ENV_B, SHOP, operation="redeploy", error=ChangedSinceReview()
        )

    assert await rig.rows("stack_redeployed") == []


async def test_every_mutation_records_its_own_action_and_no_row_holds_a_secret(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    variables = form_variables(("TOKEN", SECRET_SENTINEL, True))

    await create_then_deploy(
        rig.fresh().actions, ADMIN, ENV_B, name=SHOP, compose_text=SHOP_FILE, variables=variables
    )
    await rig.fresh().actions.update(
        ADMIN,
        ENV_B,
        SHOP,
        expected_file_digest=file_digest(SHOP_FILE),
        compose_text=SHOP_FILE + "# edited\n",
    )
    await rig.fresh().actions.stop(ADMIN, ENV_B, SHOP)
    await rig.fresh().actions.start(ADMIN, ENV_B, SHOP)
    await rig.fresh().actions.redeploy(ADMIN, ENV_B, SHOP)
    await rig.fresh().actions.remove(
        ADMIN, ENV_B, SHOP, **await removal_binding(rig, ADMIN, ENV_B, SHOP)
    )

    recorded = [(row["action"], row["result"]) for row in await rig.rows()]
    assert [action for action, _ in recorded if action.startswith("stack_")] == [
        "stack_created",
        "stack_deployed",
        "stack_updated",
        "stack_stopped",
        "stack_started",
        "stack_redeployed",
        "stack_removed",
    ]
    assert {result for _, result in recorded} == {"success"}
    deployed = (await rig.rows("stack_deployed"))[0]["detail"]
    assert len(deployed["compose_digest"]) == 64 and len(deployed["resolved_digest"]) == 64
    assert SECRET_SENTINEL not in await activity_dump(rig)


async def test_compose_below_the_floor_refuses_operations_with_both_versions_and_pages_never_spawn(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    project_box(rig.host, 1, SHOP)
    spawned: list[tuple[str, ...]] = []
    real_run = run

    async def counting_run(binary: Any, invocation: Any, **kwargs: Any) -> Any:
        spawned.append(invocation.command)
        return await real_run(binary, invocation, **kwargs)

    monkeypatch.setattr(cli, "run", counting_run)
    rig.compose.version = OLD_VERSION
    queries = rig.fresh().queries

    context = await queries.create_context(ADMIN, ENV_B)
    page = await queries.page(ADMIN, ENV_B)
    detail = await queries.detail(ADMIN, ENV_B, SHOP)
    await queries.edit_context(ADMIN, ENV_B, SHOP)
    await queries.redeploy_confirmation(ADMIN, ENV_B, SHOP)
    await queries.remove_confirmation(ADMIN, ENV_B, SHOP)
    await queries.forget_confirmation(ADMIN, ENV_B, SHOP)
    with pytest.raises(ComposeNotReady) as below:
        await rig.fresh().actions.redeploy(ADMIN, ENV_B, SHOP)
    with pytest.raises(ComposeNotReady) as validating:
        await queries.validate(ADMIN, ENV_B, name="fresh", compose_text=SHOP_FILE, variables=None)

    assert spawned == []
    assert not context.compose_ready and context.compose_floor == FLOOR_TEXT
    assert OLD_VERSION.text in (context.compose_message or "")
    assert (page.compose.installed, detail.compose.required) == (OLD_VERSION.text, FLOOR_TEXT)
    assert [r.name for r in page.items] == [SHOP]
    assert (below.value.installed, below.value.required) == (OLD_VERSION.text, FLOOR_TEXT)
    assert OLD_VERSION.text in str(below.value) and FLOOR_TEXT in str(below.value)
    assert validating.value.reason == "compose_version_unsupported"
    failed = (await rig.rows("stack_redeployed"))[0]
    assert failed["detail"]["reason"] == "compose_version_unsupported"

    rig.compose.version = ComposeVersion("v2.33.0", 2, 33, 0)
    await queries.validate(ADMIN, ENV_B, name="fresh", compose_text=SHOP_FILE, variables=None)
    assert [command[0] for command in spawned] == ["config"]


async def test_a_missing_compose_refuses_operations_as_unavailable_and_names_no_version(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    project_box(rig.host, 1, SHOP)
    rig.compose.use_binary(ComposeBinary(("/nonexistent/docker-compose",)))

    with pytest.raises(ComposeNotReady) as missing:
        await rig.fresh().actions.start(ADMIN, ENV_B, SHOP)
    context = await rig.fresh().queries.create_context(ADMIN, ENV_B)

    assert missing.value.installed is None and missing.value.required is None
    assert (await rig.rows("stack_started"))[0]["detail"]["reason"] == "compose_unavailable"
    assert context.compose_ready is False and context.compose.installed is None


async def test_a_stack_operation_and_a_container_action_exclude_each_other(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    box = project_box(rig.host, 1, SHOP)
    holder = LockHolder(UserActor.of(ADMIN), ADMIN.username, "restart")

    async with rig.engine.locks.hold(ObjectKey(ENV_B, "container", box.id), holder):
        with pytest.raises(ObjectBusy) as container_busy:
            await rig.fresh().actions.stop(ADMIN, ENV_B, SHOP)
    async with rig.engine.locks.hold(ObjectKey(ENV_B, "stack", SHOP), holder):
        with pytest.raises(ObjectBusy):
            await rig.fresh().actions.stop(ADMIN, ENV_B, SHOP)
        busy = await rig.fresh().queries.busy(ADMIN, ENV_B, SHOP)
        detail = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)

    assert container_busy.value.holder.action == "restart"
    assert busy is not None and busy.action == "restart"
    assert detail.busy is not None and detail.busy.action == "restart"
    rows = await rig.rows("stack_stopped")
    assert [r["detail"]["reason"] for r in rows] == ["object_busy", "object_busy"]
    assert rig.host.mutations() == []
    assert len(rig.engine.locks) == 0


async def test_a_standard_user_is_read_only_on_the_local_socket_even_with_a_write_grant(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE, owner_user_id=ADMIN.user_id)
    await grant_write(rig, ResourceKey(ENV_B, "stack", SHOP), ADMIN, STANDARD)

    with pytest.raises(PermissionDenied):
        await rig.fresh().actions.create(STANDARD, ENV_B, name="fresh", compose_text=SHOP_FILE)
    with pytest.raises(PermissionDenied):
        await rig.fresh().queries.validate(
            STANDARD, ENV_B, name="fresh", compose_text=SHOP_FILE, variables=None
        )
    detail = await rig.fresh().queries.detail(STANDARD, ENV_B, SHOP)
    with pytest.raises(PermissionDenied):
        await rig.fresh().actions.stop(STANDARD, ENV_B, SHOP)

    assert detail.can_write is False and detail.can_manage is False
    assert await rig.rows("stack_created") == []


async def test_the_orphan_sweep_keeps_known_and_young_directories_and_removes_old_unknown_ones(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    known = await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    base = rig.compose.files.base
    old, young = "a" * 32, "b" * 32
    for token in (known.token, old, young):
        (base / token).mkdir()
    aged = time.time() - OLD_DIRECTORY_AGE
    for token in (known.token, old):
        os.utime(base / token, (aged, aged))

    removed = await sweep_orphan_directories(
        rig.database, rig.compose.files, rig.clock, min_age_seconds=MIN_AGE
    )
    later = await sweep_orphan_directories(rig.database, rig.compose.files, rig.clock)

    assert (removed, later) == (1, 1)
    assert sorted(path.name for path in base.iterdir()) == [known.token]


SWARM_NAMESPACE_LABEL = "com.docker.stack.namespace"
NAMESPACE_CARRIERS = ["service", "config", "secret", "network", "volume", "container"]
CONTESTED = "Web"
CONTESTED_KEY = "web"
PLAIN_FILE = "services:\n  web:\n    image: nginx:1\n"


def carry_namespace(host: Any, carrier: str) -> None:
    labels = {SWARM_NAMESPACE_LABEL: CONTESTED}
    if carrier == "service":
        host.add_service(plain_spec("Web_api", Labels=labels))
    elif carrier == "config":
        host.add_config("Web_conf", labels=labels)
    elif carrier == "secret":
        host.add_secret("Web_sec", labels=labels)
    elif carrier == "volume":
        host.add_volume("Web_data", labels=labels)
    elif carrier == "container":
        host.add(f"{0xB0001:064x}", "web-task", labels=labels)
    else:
        host.add_network(
            "Web_default",
            0,
            "overlay",
            network_id=swarm_network_id(9),
            Scope="swarm",
            Labels=labels,
        )


async def stack_control_rows(rig: Rig) -> list[tuple[str, int | None, str]]:
    found = await rig.db.fetch_limited(
        "SELECT resource_id, owner_user_id, access_level FROM resource_control "
        "WHERE resource_type = 'stack' ORDER BY resource_id",
        (),
        limit=10,
    )
    return [(r["resource_id"], r["owner_user_id"], r["access_level"]) for r in found]


async def plant_swarm_control(rig: Rig, name: str) -> None:
    await rig.access.controls.create(
        ADMIN, ResourceKey(ENV_A, "stack", name), ControlSpec("read"), expected_digest=""
    )


@pytest.mark.parametrize("carrier", NAMESPACE_CARRIERS)
async def test_a_compose_create_is_refused_while_a_namespace_of_that_name_exists_on_any_object(
    compose_on_swarm_rig: Rig, stack_daemon: Any, carrier: str
) -> None:
    rig = compose_on_swarm_rig
    carry_namespace(stack_daemon, carrier)
    await plant_swarm_control(rig, CONTESTED)
    before = await stack_control_rows(rig)

    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.create(
            STANDARD, ENV_A, name=CONTESTED_KEY, compose_text=PLAIN_FILE
        )

    assert await stack_control_rows(rig) == before
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None


async def test_a_compose_create_is_refused_while_a_swarm_stack_row_folds_to_its_name(
    compose_on_swarm_rig: Rig,
) -> None:
    rig = compose_on_swarm_rig
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    await repository.insert(
        environment_id=ENV_A,
        name=CONTESTED,
        kind="swarm",
        origin="created",
        project_directory=rig.compose.files.new_token(),
        compose_file=PLAIN_FILE,
        compose_file_digest=file_digest(PLAIN_FILE),
        created_by_user_id=None,
    )
    await plant_swarm_control(rig, CONTESTED)
    before = await stack_control_rows(rig)

    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.create(
            STANDARD, ENV_A, name=CONTESTED_KEY, compose_text=PLAIN_FILE
        )

    assert await stack_control_rows(rig) == before


async def test_a_name_nothing_uses_still_creates_and_replaces_a_control_left_behind(
    compose_on_swarm_rig: Rig,
) -> None:
    rig = compose_on_swarm_rig
    await plant_swarm_control(rig, "gone")

    created = await rig.fresh().actions.create(
        STANDARD, ENV_A, name="gone", compose_text=PLAIN_FILE
    )

    assert created.stale_control_replaced and created.control_created
    [(_, owner, _)] = await stack_control_rows(rig)
    assert owner == STANDARD.user_id


async def test_a_namespace_read_that_fails_leaves_the_name_unavailable_and_writes_nothing(
    compose_on_swarm_rig: Rig, stack_daemon: Any
) -> None:
    rig = compose_on_swarm_rig
    stack_daemon.outage.add("secrets")

    with pytest.raises(EnvironmentUnavailable):
        await rig.fresh().actions.create(
            STANDARD, ENV_A, name=CONTESTED_KEY, compose_text=PLAIN_FILE
        )

    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None


async def test_without_the_namespace_check_a_create_would_take_the_control_over(
    compose_on_swarm_rig: Rig, stack_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = compose_on_swarm_rig
    carry_namespace(stack_daemon, "service")
    await plant_swarm_control(rig, CONTESTED)

    async def nothing(*args: Any, **kwargs: Any) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr("app.services.stack_queries.namespace_keys", nothing)
    created = await rig.fresh().actions.create(
        STANDARD, ENV_A, name=CONTESTED_KEY, compose_text=PLAIN_FILE
    )

    assert created.stale_control_replaced
    [(_, owner, _)] = await stack_control_rows(rig)
    assert owner == STANDARD.user_id


async def test_the_compose_list_and_json_page_carry_compose_rows_only_and_a_swarm_row_is_not_found(
    compose_on_swarm_rig: Rig,
) -> None:
    rig = compose_on_swarm_rig
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    for name, kind in (("alpha", "compose"), ("Beta", "swarm")):
        await repository.insert(
            environment_id=ENV_A,
            name=name,
            kind=kind,  # type: ignore[arg-type]
            origin="created",
            project_directory=rig.compose.files.new_token(),
            compose_file=PLAIN_FILE,
            compose_file_digest=file_digest(PLAIN_FILE),
            created_by_user_id=None,
        )

    html = await rig.fresh().queries.page(ADMIN, ENV_A)
    json_page = await rig.fresh().queries.page(ADMIN, ENV_A, include_unmanaged=False)

    assert [r.name for r in html.items] == ["alpha"]
    assert [r.name for r in json_page.items] == ["alpha"]
    with pytest.raises(ResourceNotFound):
        await rig.fresh().queries.require_stack(ADMIN, ENV_A, "Beta", "read")


async def test_saving_a_file_that_changed_since_the_edit_began_is_refused_and_writes_nothing(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, SHOP_FILE)
    began_with = file_digest(SHOP_FILE)
    newer = SHOP_FILE + "# saved by someone else\n"
    await rig.fresh().actions.update(
        ADMIN, ENV_B, SHOP, expected_file_digest=began_with, compose_text=newer
    )

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.update(
            ADMIN,
            ENV_B,
            SHOP,
            expected_file_digest=began_with,
            compose_text=SHOP_FILE + "# my edit\n",
        )

    stored = await rig.fresh().queries.detail(ADMIN, ENV_B, SHOP)
    assert stored.compose_file_digest == file_digest(newer)
    [accepted, refused] = await rig.rows("stack_updated")
    assert (accepted["result"], refused["result"]) == ("success", "failure")
    assert refused["detail"]["reason"] == "changed_since_review"


DISCLOSING_FILE = """
services:
  web:
    image: nginx:1
    environment:
      - DOCKER_HOST
x-fake:
  record: {record}
"""
OWNED_STACK = "owned"


async def test_a_redeploy_of_a_stored_file_reading_a_disclosing_variable_never_reaches_compose(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    record = tmp_path / "calls.jsonl"
    text = DISCLOSING_FILE.format(record=record)
    await seed_stack_row(rig, ENV_A, SHOP, text, owner_user_id=ADMIN.user_id)
    await seed_stack_row(rig, ENV_A, OWNED_STACK, text, owner_user_id=STANDARD.user_id)
    project_box(rig.host, 1, SHOP, "web", state="running")
    project_box(rig.host, 2, OWNED_STACK, "web", state="running")

    with pytest.raises(StackRejected) as by_administrator:
        await rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP)
    with pytest.raises(StackRejected) as by_owner:
        await rig.fresh().actions.redeploy(STANDARD, ENV_A, OWNED_STACK)

    expected = (
        "`services.web.environment[0]`: "
        "the variable DOCKER_HOST is reserved and cannot be used in a stack."
    )
    assert (by_administrator.value.reason, by_owner.value.reason) == (
        "refused_construct",
        "refused_construct",
    )
    assert by_administrator.value.fields == {"compose_file": expected}
    assert by_owner.value.fields == {"compose_file": expected}
    assert read_records(record) == []
