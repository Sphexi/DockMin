"""A Swarm stack redeployed by an automation run: what it refuses, leaves alone and records."""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.db.repositories.activity_event import DetailValue
from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services import swarm_common, swarm_stack_plan
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.image_bindings import BindingPullCredentials
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.stack_deploy import StackOperationFailed, StackRejected
from app.services.swarm_stack_plan import (
    SWARM_AUTOMATION_REFUSALS,
    StackCredentialsRefused,
    spec_differs,
)
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import authority_context, event_rows
from tests.image_support import REFERENCE, digest, register
from tests.object_support import drain_background
from tests.stack_support import Rig
from tests.swarm_stack_support import (
    NAME,
    NAMESPACE_LABEL,
    StackDaemon,
    create_stack,
    deploy_stack,
    redeploy_stack,
    shop_file,
)
from tests.updater_support import UPDATER

NOTE: dict[str, DetailValue] = {"trigger": "update", "references_updated": 1}
STACK_KEY = ObjectKey(ENV_A, "stack", NAME.lower())
OLD = digest(1)
PINNED_OLD = f"busybox:latest@{OLD}"
SHORT_WAIT = 0.05
WEB = "Shop_web"
API = "Shop_api"


def daemon_of(rig: Rig) -> StackDaemon:
    return rig.host  # type: ignore[return-value]


async def authority(rig: Rig, **options: Any) -> tuple[Any, AutomationAuthority]:
    context = await authority_context(
        rig.db, rig.clock, rig.engine.locks, environment_id=ENV_A, subsystem=UPDATER, **options
    )
    return context, context.authority()


async def deployed(rig: Rig, text: str | None = None) -> None:
    await create_stack(rig, text)
    await deploy_stack(rig)


async def automated(rig: Rig, acting: AutomationAuthority, **extra: Any) -> Any:
    return await rig.fresh().swarm_actions.redeploy(acting, ENV_A, NAME, note=NOTE, **extra)


def moved(daemon: StackDaemon, name: str, image: str) -> None:
    daemon.service(name)["TaskTemplate"]["ContainerSpec"]["Image"] = image


def sends(daemon: StackDaemon, since: int) -> list[str]:
    return daemon.mutating()[since:]


async def refused(rig: Rig, acting: AutomationAuthority) -> tuple[StackRejected, dict[str, Any]]:
    with pytest.raises(StackRejected) as raised:
        await automated(rig, acting)
    rows = await event_rows(rig.db, "stack_redeployed")
    return raised.value, rows[-1]


async def test_an_undisturbed_stack_is_redeployed_by_sending_nothing_and_the_row_names_the_run(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    before = len(daemon.mutating())
    context, acting = await authority(rig)

    outcome = await automated(rig, acting)

    assert outcome.updated == () and len(outcome.unchanged) == 2
    assert sends(daemon, before) == []
    [row] = await event_rows(rig.db, "stack_redeployed")
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        UPDATER,
    )
    assert row["rule_id"] == context.actor.rule_id and row["job_run_id"] is not None
    assert (row["detail"]["trigger"], row["detail"]["remove_orphans"]) == ("update", False)
    assert rig.engine.locks.holder_of(STACK_KEY) is None
    assert context.owner_gate.checked[0] == STACK_KEY


async def test_a_moved_tag_updates_only_the_service_whose_image_changed(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, f"busybox:latest@{digest(7)}")
    _, acting = await authority(rig)

    outcome = await automated(rig, acting)

    assert outcome.updated == (f"service:{WEB}",)
    assert outcome.unchanged == (f"service:{API}",)


async def test_the_defaults_the_engine_fills_in_are_neither_drift_nor_a_reason_to_send_a_member(
    defaulting_stack_rig: Rig,
) -> None:
    rig, daemon = defaulting_stack_rig, daemon_of(defaulting_stack_rig)
    await deployed(rig)
    stored = daemon.service(WEB)
    assert "Runtime" not in stored["TaskTemplate"]
    before = len(daemon.mutating())
    _, acting = await authority(rig)

    outcome = await automated(rig, acting)

    assert outcome.updated == () and len(outcome.unchanged) == 2
    assert sends(daemon, before) == []


async def test_a_users_second_redeploy_of_an_unchanged_stack_sends_no_member_again(
    defaulting_stack_rig: Rig,
) -> None:
    rig, daemon = defaulting_stack_rig, daemon_of(defaulting_stack_rig)
    await deployed(rig)
    await redeploy_stack(rig)
    before = len(daemon.mutating())

    second = await redeploy_stack(rig)

    assert second.state == "succeeded"
    assert [m for m in sends(daemon, before) if "/update" in m] == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"remove_orphans": True},
        {"expected_plan": "x"},
        {"expected_file_digest": "x"},
        {"expected_inputs_digest": "x"},
        {"choices": {"busybox:latest": object()}},
    ],
    ids=["orphans", "plan", "file-digest", "inputs-digest", "choices"],
)
async def test_an_automation_run_cannot_remove_orphans_or_bind_a_review_or_choose_a_set(
    swarm_stack_rig: Rig, arguments: dict[str, Any]
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    before = len(daemon.mutating())
    _, acting = await authority(rig)

    with pytest.raises(ValueError):
        await automated(rig, acting, **arguments)

    assert sends(daemon, before) == []


async def test_a_plan_for_an_automation_run_cannot_ask_for_the_collision_check(
    swarm_stack_rig: Rig,
) -> None:
    rig = swarm_stack_rig
    await deployed(rig)
    _, acting = await authority(rig)
    planner = rig.fresh().swarm_planner
    row = await planner.require_stack(ADMIN, ENV_A, NAME, "write")

    with pytest.raises(ValueError):
        await planner.deploy_plan(acting, ENV_A, row, remove_orphans=False, check_collisions=True)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"expected_file_digest": "a"},
        {"expected_plan": "a"},
        {"expected_file_digest": "a", "expected_plan": "b"},
        {
            "expected_file_digest": "a",
            "expected_inputs_digest": "c",
            "expected_plan": "b",
            "note": NOTE,
        },
    ],
    ids=["neither", "file-only", "plan-only", "inputs-missing", "note"],
)
async def test_a_user_must_bind_all_reviewed_values_and_cannot_attach_a_note(
    swarm_stack_rig: Rig, arguments: dict[str, Any]
) -> None:
    rig = swarm_stack_rig
    await deployed(rig)

    with pytest.raises(ValueError):
        await rig.fresh().swarm_actions.redeploy(ADMIN, ENV_A, NAME, **arguments)


async def edit_the_file(rig: Rig, daemon: StackDaemon) -> None:
    await rig.fresh().swarm_actions.update(
        ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3), variables=None
    )


async def leave_a_config(rig: Rig, daemon: StackDaemon) -> None:
    daemon.add_config("Shop_old_0123456789ab", labels={NAMESPACE_LABEL: NAME})


async def leave_a_network(rig: Rig, daemon: StackDaemon) -> None:
    daemon.add_network("Shop_spare", 99, "overlay", Labels={NAMESPACE_LABEL: NAME})


async def lose_a_service(rig: Rig, daemon: StackDaemon) -> None:
    del daemon.services[daemon.service_id(API)]


async def scale_a_service(rig: Rig, daemon: StackDaemon) -> None:
    daemon.service(WEB)["Mode"] = {"Replicated": {"Replicas": 5}}


async def add_an_environment_variable(rig: Rig, daemon: StackDaemon) -> None:
    daemon.service(WEB)["TaskTemplate"]["ContainerSpec"]["Env"] = ["EXTRA=1"]


async def fail_the_digest_lookup(rig: Rig, daemon: StackDaemon) -> None:
    daemon.distribution_fails = True


type Setup = Callable[[Rig, StackDaemon], Awaitable[None]]

REFUSALS: list[tuple[str, Setup, str, dict[str, int]]] = [
    ("edited-file", edit_the_file, "pending_changes", {}),
    ("leftover-config", leave_a_config, "would_remove", {"remove_configs": 1}),
    ("spare-network", leave_a_network, "would_remove", {"remove_networks": 1}),
    ("lost-service", lose_a_service, "would_create", {"create_services": 1}),
    ("scaled-service", scale_a_service, "drifted", {"drifted_services": 1}),
    ("edited-service", add_an_environment_variable, "drifted", {"drifted_services": 1}),
    ("unresolved-digest", fail_the_digest_lookup, "unresolved", {"unresolved_references": 1}),
]


@pytest.mark.parametrize(
    ("setup", "reason", "counts"),
    [row[1:] for row in REFUSALS],
    ids=[row[0] for row in REFUSALS],
)
async def test_a_redeploy_that_would_remove_create_overwrite_or_send_a_bare_tag_is_refused_first(
    swarm_stack_rig: Rig, setup: Setup, reason: str, counts: dict[str, int]
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    await setup(rig, daemon)
    before = len(daemon.mutating())
    _, acting = await authority(rig)

    error, row = await refused(rig, acting)

    assert error.reason == reason
    assert sends(daemon, before) == []
    assert (row["result"], row["actor_subsystem"], row["detail"]["reason"]) == (
        "failure",
        UPDATER,
        reason,
    )
    assert {key: row["detail"].get(key) for key in counts} == counts
    assert rig.engine.locks.holder_of(STACK_KEY) is None


def test_every_refusal_an_automation_run_can_meet_is_in_the_published_set() -> None:
    assert {row[2] for row in REFUSALS} <= SWARM_AUTOMATION_REFUSALS
    assert {
        "pending_changes",
        "would_remove",
        "would_create",
        "drifted",
        "unresolved",
    } == SWARM_AUTOMATION_REFUSALS


async def test_a_removal_is_reported_ahead_of_a_creation_and_both_counts_are_in_the_row(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    await leave_a_config(rig, daemon)
    await lose_a_service(rig, daemon)
    _, acting = await authority(rig)

    error, row = await refused(rig, acting)

    assert error.reason == "would_remove"
    assert (row["detail"]["remove_configs"], row["detail"]["create_services"]) == (1, 1)


async def test_drift_is_reported_ahead_of_an_unresolved_digest(swarm_stack_rig: Rig) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    daemon.distribution_fails = True
    moved(daemon, WEB, "alpine:3")
    _, acting = await authority(rig)

    error, _ = await refused(rig, acting)

    assert error.reason == "drifted"


async def test_a_user_still_redeploys_a_stack_whose_digest_lookup_fails(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    daemon.distribution_fails = True

    outcome = await redeploy_stack(rig)

    assert outcome.state == "succeeded"


async def test_a_file_saved_after_the_stack_was_read_but_before_it_was_locked_is_pending(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    before = len(daemon.mutating())
    _, acting = await authority(rig)
    actions = rig.fresh().swarm_actions
    hold = actions._hold
    edited: list[None] = []

    async def hold_then_edit(*arguments: Any, **keywords: Any) -> None:
        await hold(*arguments, **keywords)
        if not edited:
            edited.append(None)
            await rig.db.execute(
                "UPDATE stack SET compose_file_digest = ? WHERE name = ?", ("f" * 64, NAME)
            )

    actions._hold = hold_then_edit  # type: ignore[method-assign]

    with pytest.raises(StackRejected) as raised:
        await actions.redeploy(acting, ENV_A, NAME, note=NOTE)

    assert raised.value.reason == "pending_changes" and sends(daemon, before) == []


@pytest.mark.parametrize(
    "running",
    [
        f"alpine:3@{OLD}",
        f"busybox:other@{OLD}",
        "busybox:latest",
        "busybox",
        "sha256:" + "a" * 64,
        f"busybox@{OLD}",
        "Busybox::",
    ],
    ids=["other-repository", "other-tag", "unpinned", "bare", "image-id", "digest-only", "invalid"],
)
async def test_a_running_image_that_is_not_a_pinned_form_of_the_files_reference_is_drift(
    swarm_stack_rig: Rig, running: str
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, running)
    before = len(daemon.mutating())
    _, acting = await authority(rig)

    error, row = await refused(rig, acting)

    assert error.reason == "drifted" and sends(daemon, before) == []
    assert row["detail"]["drifted_services"] == 1


@pytest.mark.parametrize(
    "running",
    [PINNED_OLD, f"docker.io/library/busybox:latest@{OLD}"],
    ids=["as-written", "spelled-out"],
)
async def test_a_pinned_form_of_the_files_reference_is_not_drift(
    swarm_stack_rig: Rig, running: str
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, running)
    _, acting = await authority(rig)

    outcome = await automated(rig, acting)

    assert outcome.updated == (f"service:{WEB}",)


async def test_a_member_edited_between_the_plan_and_the_send_is_not_overwritten(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, PINNED_OLD)
    _, acting = await authority(rig)
    actions = rig.fresh().swarm_actions
    plan = actions._planner.deploy_plan
    planned: list[None] = []

    async def planned_then_edited(*arguments: Any, **keywords: Any) -> Any:
        found = await plan(*arguments, **keywords)
        planned.append(None)
        if len(planned) == 2:
            daemon.services[daemon.service_id(WEB)]["Version"]["Index"] += 1
        return found

    actions._planner.deploy_plan = planned_then_edited  # type: ignore[method-assign]
    before = len(daemon.mutating())

    with pytest.raises(StackOperationFailed) as raised:
        await actions.redeploy(acting, ENV_A, NAME, note=NOTE)

    assert raised.value.reason == "changed_since_review" and sends(daemon, before) == []
    [row] = await event_rows(rig.db, "stack_redeployed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "changed_since_review")


async def test_pins_resolve_with_no_principal_and_no_choice_for_an_automation_run_only(
    swarm_stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = swarm_stack_rig
    await deployed(rig)
    calls: list[tuple[str, Any, Any]] = []
    resolve = BindingPullCredentials.resolve

    async def spy(
        self: Any, environment_id: int, reference: str, *, principal: Any, choice: Any = None
    ) -> Any:
        calls.append((reference, principal, choice))
        return await resolve(self, environment_id, reference, principal=principal, choice=choice)

    monkeypatch.setattr(BindingPullCredentials, "resolve", spy)
    _, acting = await authority(rig)

    await automated(rig, acting)
    automated_calls = list(calls)
    calls.clear()
    await redeploy_stack(rig)

    assert automated_calls and all(p is None and c is None for _, p, c in automated_calls)
    assert calls and all(p == ADMIN for _, p, _ in calls)


async def test_a_bound_set_the_owner_may_not_use_refuses_the_redeploy_before_any_write(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    set_id = await register(rig, "main")
    await deployed(rig, shop_file(image=REFERENCE))
    asked: list[int] = []

    async def refuse(bound: int) -> None:
        asked.append(bound)
        raise CredentialSetUnavailable("not_granted_user")

    _, acting = await authority(rig)
    before = len(daemon.mutating())

    with pytest.raises(StackRejected):
        await automated(rig, dataclasses.replace(acting, authorise_credential_set=refuse))

    assert asked == [set_id] and sends(daemon, before) == []


CREDENTIAL_ERRORS = [
    (
        {"alpine:3": CredentialSetMismatch(), "busybox:latest": CredentialChoiceRequired(())},
        {"alpine:3": "credential_mismatch", "busybox:latest": "not_bound"},
        "credential_choice_required",
    ),
    (
        {
            "alpine:3": BindingCredentialMissing(),
            "busybox:latest": CredentialSetUnavailable("not_granted_environment"),
        },
        {"alpine:3": "credential_missing", "busybox:latest": "credential_unavailable"},
        None,
    ),
]


@pytest.mark.parametrize(
    ("errors", "per_reference", "reason"),
    CREDENTIAL_ERRORS,
    ids=["mismatch-and-unbound", "missing-and-unavailable"],
)
async def test_each_reference_a_redeploy_refuses_keeps_its_own_reason(
    swarm_stack_rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    errors: dict[str, Exception],
    per_reference: dict[str, str],
    reason: str | None,
) -> None:
    rig = swarm_stack_rig
    await deployed(rig, shop_file(image="alpine:3"))
    real = swarm_common.pin_image

    async def refusing(
        resolver: Any, client: Any, actor: Any, env: int, image: str, *a: Any, **k: Any
    ) -> Any:
        if image in errors:
            raise errors[image]
        return await real(resolver, client, actor, env, image, *a, **k)

    monkeypatch.setattr(swarm_stack_plan, "pin_image", refusing)
    _, acting = await authority(rig)

    with pytest.raises(StackCredentialsRefused) as raised:
        await automated(rig, acting)

    assert isinstance(raised.value, StackRejected)
    assert raised.value.detail["refused_references"] == per_reference
    assert set(raised.value.fields) == set(per_reference)
    assert reason is None or raised.value.reason == reason


async def test_a_redeploy_refused_for_a_busy_stack_or_member_or_a_suppressed_run_writes_no_row(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    suppressions = SuppressionRegistry(rig.clock)
    _, acting = await authority(rig, lock_wait=SHORT_WAIT, suppressions=suppressions)
    locks = rig.engine.locks
    user = LockHolder(UserActor.of(ADMIN), ADMIN.username, "redeploy")
    member = ObjectKey(ENV_A, "service", daemon.service_id(WEB))

    async with locks.hold(STACK_KEY, user):
        with pytest.raises(ObjectBusy):
            await automated(rig, acting)
    async with locks.hold(member, user):
        with pytest.raises(ObjectBusy):
            await automated(rig, acting)
    suppressions.note(STACK_KEY)
    with pytest.raises(RunSkipped):
        await automated(rig, acting)

    assert await event_rows(rig.db, "stack_redeployed") == []
    assert locks.holder_of(STACK_KEY) is None


async def test_an_owner_who_may_not_write_a_member_is_refused_as_restricted(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    _, acting = await authority(rig)
    before = len(daemon.mutating())

    async def deny(key: ObjectKey) -> None:
        if key.object_type == "service":
            raise PermissionDenied("insufficient access")

    standard = dataclasses.replace(acting, owner_is_administrator=False, authorise=deny)

    with pytest.raises(StackRejected) as raised:
        await automated(rig, standard)

    assert raised.value.reason == "restricted" and sends(daemon, before) == []
    [row] = await event_rows(rig.db, "stack_redeployed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "restricted")


async def test_a_standard_owner_with_access_to_every_object_redeploys_and_each_is_authorised(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, f"busybox:latest@{digest(7)}")
    _, acting = await authority(rig)
    seen: list[ObjectKey] = []

    async def record(key: ObjectKey) -> None:
        seen.append(key)

    standard = dataclasses.replace(
        acting, owner_is_administrator=False, authorise=record, authorise_read=record
    )

    outcome = await automated(rig, standard)

    assert outcome.updated == (f"service:{WEB}",)
    assert {key.object_type for key in seen} >= {"stack", "service", "network", "config", "secret"}


async def test_a_cancelled_redeploy_leaves_an_interrupted_row_under_the_subsystem_and_unlocks(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await deployed(rig)
    moved(daemon, WEB, f"busybox:latest@{digest(7)}")
    context, acting = await authority(rig)
    daemon.gate = asyncio.Event()
    task = asyncio.create_task(automated(rig, acting))
    await asyncio.wait_for(daemon.arrived.wait(), 10)

    task.cancel()
    daemon.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    rows = await event_rows(rig.db, "stack_redeployed")
    assert rows and {(r["actor_type"], r["actor_subsystem"]) for r in rows} == {
        ("subsystem", UPDATER)
    }
    assert {r["rule_id"] for r in rows} == {context.actor.rule_id}
    assert rows[-1]["detail"]["interrupted"] is True
    assert rig.engine.locks.holder_of(STACK_KEY) is None


NETWORK_ID = "n" * 25
NETWORKS = {"front": NETWORK_ID}


def spec(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "Name": "web",
        "TaskTemplate": {
            "ContainerSpec": {"Image": "busybox:1"},
            "Networks": [{"Target": "front"}],
        },
        "Mode": {"Replicated": {"Replicas": 2}},
    }
    document.update(changes)
    return document


def with_defaults(document: dict[str, Any]) -> dict[str, Any]:
    document["TaskTemplate"]["Runtime"] = "container"
    document["TaskTemplate"]["ContainerSpec"]["Isolation"] = "default"
    document["TaskTemplate"]["Networks"] = [{"Target": NETWORK_ID}]
    return document


def scaled() -> dict[str, Any]:
    return spec(Mode={"Replicated": {"Replicas": 3}})


def other_runtime() -> dict[str, Any]:
    document = with_defaults(spec())
    document["TaskTemplate"]["Runtime"] = "other"
    return document


def other_isolation() -> dict[str, Any]:
    document = with_defaults(spec())
    document["TaskTemplate"]["ContainerSpec"]["Isolation"] = "hyperv"
    return document


def another_network() -> dict[str, Any]:
    document = with_defaults(spec())
    document["TaskTemplate"]["Networks"] = [{"Target": "o" * 25}]
    return document


def an_extra_key() -> dict[str, Any]:
    document = with_defaults(spec())
    document["TaskTemplate"]["ContainerSpec"]["Env"] = ["A=1"]
    return document


def test_a_held_spec_with_only_the_engines_defaults_added_does_not_differ() -> None:
    assert not spec_differs(spec(), with_defaults(spec()), NETWORKS)
    assert not spec_differs(with_defaults(spec()), spec(), NETWORKS)


@pytest.mark.parametrize(
    "held",
    [scaled, other_runtime, other_isolation, another_network, an_extra_key],
    ids=["replicas", "runtime", "isolation", "network", "extra-key"],
)
def test_any_difference_beyond_the_three_engine_defaults_counts(
    held: Callable[[], dict[str, Any]],
) -> None:
    assert spec_differs(spec(), held(), NETWORKS)


async def test_a_moved_bound_reference_rebinds_under_the_subsystem_and_never_a_user(
    swarm_stack_rig: Rig,
) -> None:
    rig, daemon = swarm_stack_rig, daemon_of(swarm_stack_rig)
    await register(rig, "main")
    await deployed(rig, shop_file(image=REFERENCE))
    known = len(await event_rows(rig.db))
    moved(daemon, WEB, f"{REFERENCE}@{digest(7)}")
    _, acting = await authority(rig)

    await automated(rig, acting)

    new = (await event_rows(rig.db))[known:]
    assert {(r["actor_type"], r["actor_subsystem"]) for r in new} == {("subsystem", UPDATER)}
    assert new[-1]["detail"]["binding"] == "kept"
