"""container-updater on a Swarm stack: one redeploy, what it refuses, and what holds the stack."""

from datetime import timedelta
from typing import Any

import pytest

import app.subsystems.container_updater as updater
import app.subsystems.container_updater_swarm as swarm_path
from app.db.repositories.clocked import format_timestamp
from app.services.access import ResourceKey
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.image_bindings import BindingPullCredentials
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import StackRejected
from app.services.swarm_stack_plan import StackCredentialsRefused, SwarmStackPlanner
from tests.access_support import ADMIN, ENV_A, STANDARD, digest_of
from tests.automation_support import counts
from tests.docker_engine.swarm_engine import DIGEST
from tests.image_support import REFERENCE, digest, move_sets_elsewhere, register
from tests.registry_support import OTHER_ADDRESS, bind_image
from tests.swarm_stack_support import NAME, NAMESPACE_LABEL
from tests.swarm_updater_support import (
    NEW_DIGEST,
    STACK,
    UPDATE,
    Shop,
    rows,
    stack_images,
)
from tests.updater_support import RECEIVER, UPDATER, as_webhook

REPORT = {"action": "report", "suppression_seconds": 0}
OTHER_REFERENCE = f"{REFERENCE.rsplit(':', 1)[0]}:other"
WEB = "Shop_web"
API = "Shop_api"
SHORT_PAUSE = 5
SECOND_PASS_SECONDS = 61
MANY_SERVICES = 1
BUDGET_RUNNING_OUT = timedelta(seconds=-1)


def rewritten(shop: Shop) -> list[str]:
    return [m for m in shop.host.mutating() if "/update" in m]


async def test_a_moved_tag_redeploys_the_stack_updating_only_the_service_that_runs_it(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    api_tasks = set(shop.host.service_tasks(shop.member_id(API)))

    result = await shop.subject.run(job)

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    [row] = rows(shop.subject, "stack_redeployed")
    assert (row["actor_subsystem"], row["rule_id"], row["target_id"]) == (UPDATER, rule_id, NAME)
    assert (row["detail"]["trigger"], row["detail"]["references_updated"]) == ("update", 1)
    assert row["detail"]["remove_orphans"] is False
    images = stack_images(shop.host)
    assert images[WEB].endswith(NEW_DIGEST) and not images[API].endswith(NEW_DIGEST)
    assert {
        t
        for t, v in shop.host.tasks.items()
        if v["ServiceID"] == shop.member_id(API) and v["DesiredState"] == "running"
    } == api_tasks


async def test_a_report_and_a_dry_run_name_the_stack_and_write_and_send_nothing(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    _, job = await shop.subject.rule(STACK, UPDATE)
    _, report = await shop.subject.rule(STACK, REPORT, name="report")
    before = shop.sent()

    dry = await shop.subject.run(job, dry=True)
    reported = await shop.subject.run(report)

    assert counts(dry)["would_update"] == f"stack {NAME}" and counts(dry)["would_update_count"] == 1
    assert counts(reported)["update_available"] == 1
    assert shop.sent() == before and rows(shop.subject) == []


async def test_a_stack_with_nothing_to_update_is_not_redeployed(swarm_shop: Shop) -> None:
    shop = swarm_shop
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert result.outcome == "success" and "updated" not in counts(result)
    assert shop.sent() == before and rows(shop.subject) == []


async def test_the_start_budget_defers_a_redeploy_it_has_no_time_for(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    monkeypatch.setattr(updater, "START_BUDGET", BUDGET_RUNNING_OUT)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert counts(result)["deferred"] == 1
    assert shop.sent() == before and rows(shop.subject) == []


async def test_a_stack_with_more_services_than_the_cap_is_deferred_untouched(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    monkeypatch.setattr(swarm_path, "MAX_SERVICES", MANY_SERVICES)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert counts(result)["deferred"] == 1 and shop.sent() == before


async def test_a_stack_with_more_references_than_the_cap_is_deferred_untouched(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    monkeypatch.setattr(updater, "MAX_REFERENCES", MANY_SERVICES)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert counts(result)["deferred"] == 1 and shop.sent() == before


async def stop_the_tasks(shop: Shop) -> None:
    for task in shop.host.tasks.values():
        task["Status"]["State"] = "failed"


async def edit_the_file(shop: Shop) -> None:
    await shop.rig.db.execute(
        "UPDATE stack SET compose_file_digest = ? WHERE name = ?", ("f" * 64, NAME)
    )


async def edit_a_variable(shop: Shop) -> None:
    await shop.rig.db.execute("UPDATE stack SET inputs_digest = ? WHERE name = ?", ("f" * 64, NAME))


async def pause_a_member(shop: Shop) -> None:
    shop.host.services[shop.member_id(WEB)]["UpdateStatus"] = {"State": "paused"}


async def update_a_member(shop: Shop) -> None:
    shop.host.services[shop.member_id(WEB)]["UpdateStatus"] = {"State": "updating"}


async def make_a_member_a_job(shop: Shop) -> None:
    shop.host.services[shop.member_id(WEB)]["Spec"]["Mode"] = {"ReplicatedJob": {}}


STACK_SKIPS = [
    ("edited-file", edit_the_file, "pending_changes"),
    ("edited-variable", edit_a_variable, "pending_changes"),
    ("paused-member", pause_a_member, "update_paused"),
    ("updating-member", update_a_member, "update_in_progress"),
    ("job-member", make_a_member_a_job, "job_mode"),
    ("no-running-task", stop_the_tasks, "not_running"),
]


@pytest.mark.parametrize(
    ("change", "skip"), [row[1:] for row in STACK_SKIPS], ids=[row[0] for row in STACK_SKIPS]
)
async def test_a_stack_in_a_state_the_updater_must_leave_alone_is_counted_and_not_touched(
    swarm_shop: Shop, change: Any, skip: str
) -> None:
    shop = swarm_shop
    shop.move()
    await change(shop)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert (result.outcome, counts(result)[skip]) == ("success", 1)
    assert rows(shop.subject) == [] and shop.sent() == before


async def test_a_set_that_does_not_fit_refuses_the_stack_once_under_its_name_and_holds_it(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    await move_sets_elsewhere(shop.rig.db)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    first = await shop.subject.run(job)
    again = await shop.subject.run(job)

    [row] = rows(shop.subject, "image_update_refused")
    assert first.reason == "credential_mismatch"
    assert (row["target_type"], row["target_id"]) == ("stack", NAME)
    assert rows(shop.subject, "stack_redeployed") == [] and shop.sent() == before
    assert counts(again)["held"] == 1 and len(rows(shop.subject, "image_update_refused")) == 1


async def bind_to_another_registry(shop: Shop, reference: str) -> None:
    """Bind the reference to a set at another address, so the binding fails a pull's checks."""
    elsewhere = await register(shop.rig, "elsewhere", address=OTHER_ADDRESS)
    await bind_image(shop.rig.db, elsewhere, ENV_A, reference)


async def test_a_credential_the_redeploy_itself_refuses_becomes_a_held_refusal(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    await bind_to_another_registry(shop, OTHER_REFERENCE)

    async def unbound(self: Any, *arguments: Any, **keywords: Any) -> Any:
        raise StackRejected({OTHER_REFERENCE: "x"}, "credential_unavailable")

    monkeypatch.setattr(SwarmStackPlanner, "_pins", unbound)
    _, job = await shop.subject.rule(STACK, UPDATE)

    first = await shop.subject.run(job)
    again = await shop.subject.run(job)

    [row] = rows(shop.subject, "image_update_refused")
    assert first.outcome == "failure" and row["detail"]["reason"] == "credential_unavailable"
    assert counts(again)["held"] == 1


async def test_each_reference_the_redeploy_refuses_keeps_its_own_reason_in_the_row(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    await bind_to_another_registry(shop, OTHER_REFERENCE)

    async def refuse(self: Any, *arguments: Any, **keywords: Any) -> Any:
        raise StackCredentialsRefused(
            {REFERENCE: "x", OTHER_REFERENCE: "y"},
            "credential_mismatch",
            {REFERENCE: "credential_mismatch", OTHER_REFERENCE: "credential_unavailable"},
        )

    monkeypatch.setattr(SwarmStackPlanner, "_pins", refuse)
    _, job = await shop.subject.rule(STACK, UPDATE)

    await shop.subject.run(job)
    again = await shop.subject.run(job)

    [row] = rows(shop.subject, "image_update_refused")
    signature = row["detail"]["refused"]
    assert f"{REFERENCE}=credential_mismatch" in signature
    assert f"{OTHER_REFERENCE}=credential_unavailable" in signature
    assert counts(again)["held"] == 1 and len(rows(shop.subject, "image_update_refused")) == 1


async def hand_refusal(shop: Shop, rule_id: int, signature: str) -> None:
    await ActivityRecorder(shop.rig.db, shop.rig.runtime.clock).record_failure(
        SubsystemActor(UPDATER, rule_id, None, "Heal web"),
        action="image_update_refused",
        target_type="stack",
        target_id=NAME,
        environment_id=ENV_A,
        reason="credential_unavailable",
        detail={"name": f"stack {NAME}", "reference": REFERENCE, "refused": signature},
        message="x",
    )


@pytest.mark.parametrize(
    "signature",
    [
        "x" * updater.SIGNATURE_LIMIT,
        f"{REFERENCE}=credential_unavailable,{OTHER_REFERENCE}=credential_",
        REFERENCE,
        f"{REFERENCE}=zzz",
    ],
    ids=["cut-at-the-limit", "truncated-item", "no-reason", "unknown-reason"],
)
async def test_a_refusal_whose_signature_was_cut_or_cannot_be_read_stays_held(
    swarm_shop: Shop, signature: str
) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    await hand_refusal(shop, rule_id, signature)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert counts(result)["held"] == 1 and shop.sent() == before


async def test_a_stored_refusal_naming_a_retired_reason_holds_nothing(swarm_shop: Shop) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    await hand_refusal(shop, rule_id, f"{REFERENCE}=not_bound,{OTHER_REFERENCE}=credential_missing")

    result = await shop.subject.run(job)

    assert "held" not in counts(result)
    assert len(rows(shop.subject, "stack_redeployed")) == 1


async def test_a_refusal_is_released_once_the_set_is_granted_to_the_environment_again(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    stranger = await register(shop.rig, "stranger", environments=())
    await bind_image(shop.rig.db, stranger, ENV_A, OTHER_REFERENCE)
    await hand_refusal(shop, rule_id, f"{OTHER_REFERENCE}=credential_unavailable")
    before = shop.sent()

    held = await shop.subject.run(job)
    sent_while_held = shop.sent()
    await shop.rig.db.execute(
        "INSERT INTO registry_environment_access (registry_credential_id, environment_id) "
        "VALUES (?, ?)",
        (stranger, ENV_A),
    )
    freed = await shop.subject.run(job)

    assert counts(held)["held"] == 1 and sent_while_held == before
    assert "held" not in counts(freed)


async def test_only_a_refusal_of_the_resolver_counts_when_judging_a_hold_again(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    await hand_refusal(shop, rule_id, f"{REFERENCE}=credential_unavailable")

    async def broken(self: Any, *arguments: Any, **keywords: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(BindingPullCredentials, "check_usable", broken)

    with pytest.raises(RuntimeError):
        await shop.subject.run(job)


async def leave_a_config(shop: Shop) -> None:
    shop.host.add_config(f"{NAME.lower()}_old_0123456789ab", labels={NAMESPACE_LABEL: NAME})


async def lose_a_member(shop: Shop) -> None:
    del shop.host.services[shop.member_id(API)]


async def scale_a_member(shop: Shop) -> None:
    shop.host.service(WEB)["Mode"]["Replicated"]["Replicas"] = 5


ACT_TIME_REFUSALS = [
    ("leftover-config", leave_a_config, "would_remove", {"remove_configs": 1}),
    ("lost-member", lose_a_member, "would_create", {"create_services": 1}),
    ("scaled-member", scale_a_member, "drifted", {"drifted_services": 1}),
]


@pytest.mark.parametrize(
    ("setup", "reason", "detail"),
    [row[1:] for row in ACT_TIME_REFUSALS],
    ids=[row[0] for row in ACT_TIME_REFUSALS],
)
async def test_a_redeploy_that_would_remove_create_or_overwrite_fails_the_run_once_and_is_held(
    swarm_shop: Shop, setup: Any, reason: str, detail: dict[str, int]
) -> None:
    shop = swarm_shop
    shop.move()
    await setup(shop)
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    first = await shop.subject.run(job)
    shop.subject.advance(SHORT_PAUSE)
    again = await shop.subject.run(job)

    [row] = rows(shop.subject, "stack_redeployed")
    assert (first.outcome, first.reason) == ("failure", reason)
    assert shop.sent() == before
    assert row["detail"]["reason"] == reason
    assert {key: row["detail"].get(key) for key in detail} == detail
    assert counts(again)["held"] == 1 and len(rows(shop.subject, "stack_redeployed")) == 1


async def test_a_restricted_stack_the_owner_may_not_redeploy_is_held_after_its_refusal(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    await ActivityRecorder(shop.rig.db, shop.rig.runtime.clock).record_failure(
        SubsystemActor(UPDATER, rule_id, None, "Heal web"),
        action="stack_redeployed",
        target_type="stack",
        target_id=NAME,
        environment_id=ENV_A,
        reason="restricted",
        message="x",
    )

    result = await shop.subject.run(job)

    assert counts(result)["held"] == 1


def engine_time(shop: Shop, seconds: float = 0) -> str:
    moment = shop.subject.clock() + timedelta(seconds=seconds)
    return format_timestamp(moment).replace("Z", ".123456789Z")


async def user_acts(shop: Shop, target_type: str, target_id: str) -> None:
    await shop.rig.db.execute(
        "INSERT INTO activity_event (category, actor_type, actor_user_id, environment_id, "
        "target_type, target_id, action, result, occurred_at) "
        "VALUES ('user_action', 'user', ?, ?, ?, ?, 'a', 'success', ?)",
        (ADMIN.user_id, ENV_A, target_type, target_id, format_timestamp(shop.subject.clock())),
    )


async def rolled_back_stack(shop: Shop) -> tuple[int, int]:
    """Redeploy onto a new digest, then let Swarm roll the first member back."""
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)
    first = await shop.subject.run(job)
    assert counts(first)["updated"] == 1
    shop.subject.advance(30)
    service = shop.host.services[shop.member_id(WEB)]
    service["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"] = f"{REFERENCE}@{DIGEST}"
    service["UpdateStatus"] = {
        "State": "rollback_completed",
        "StartedAt": engine_time(shop, -25),
        "CompletedAt": engine_time(shop),
    }
    service.pop("PreviousSpec", None)
    service["Version"]["Index"] += 1
    shop.subject.advance(30)
    return rule_id, job


async def test_a_stack_whose_member_swarm_rolled_back_is_not_redeployed_with_the_same_digest(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    _, job = await rolled_back_stack(shop)
    redeploys = len(rows(shop.subject, "stack_redeployed"))
    before = shop.sent()

    again = await shop.subject.run(job)

    assert counts(again)["held"] == 1
    assert len(rows(shop.subject, "stack_redeployed")) == redeploys and shop.sent() == before


async def test_a_new_registry_digest_is_a_new_attempt_for_a_stack_swarm_rolled_back(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    _, job = await rolled_back_stack(shop)
    shop.move(digest(77))
    shop.subject.advance(SECOND_PASS_SECONDS)

    third = await shop.subject.run(job)

    assert "held" not in counts(third) and counts(third)["updated"] == 1


async def test_a_users_action_on_the_member_releases_the_hold(swarm_shop: Shop) -> None:
    shop = swarm_shop
    _, job = await rolled_back_stack(shop)
    held = await shop.subject.run(job)
    await user_acts(shop, "service", shop.member_id(WEB))
    shop.subject.advance(SHORT_PAUSE)

    released = await shop.subject.run(job)

    assert counts(held)["held"] == 1 and "held" not in counts(released)


async def test_saving_the_rule_releases_the_hold(swarm_shop: Shop) -> None:
    shop = swarm_shop
    rule_id, job = await rolled_back_stack(swarm_shop)
    held = await shop.subject.run(job)
    await shop.rig.db.execute(
        "UPDATE automation_rule SET updated_at = ? WHERE id = ?",
        (format_timestamp(shop.subject.clock()), rule_id),
    )
    shop.subject.advance(SHORT_PAUSE)

    released = await shop.subject.run(job)

    assert counts(held)["held"] == 1 and "held" not in counts(released)


async def test_the_digest_the_engine_kept_in_the_previous_spec_decides_what_failed(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    _, job = await rolled_back_stack(shop)
    service = shop.host.services[shop.member_id(WEB)]
    previous = {**service["Spec"], "TaskTemplate": {**service["Spec"]["TaskTemplate"]}}
    previous["TaskTemplate"]["ContainerSpec"] = {
        **service["Spec"]["TaskTemplate"]["ContainerSpec"],
        "Image": f"{REFERENCE}@{digest(77)}",
    }
    service["PreviousSpec"] = previous

    again = await shop.subject.run(job)

    assert "held" not in counts(again)


async def test_a_users_action_on_the_member_releases_the_drifted_hold(swarm_shop: Shop) -> None:
    shop = swarm_shop
    shop.move()
    await scale_a_member(shop)
    _, job = await shop.subject.rule(STACK, UPDATE)
    await shop.subject.run(job)
    held = await shop.subject.run(job)
    shop.subject.advance(SHORT_PAUSE)
    await user_acts(shop, "service", shop.member_id(WEB))
    shop.subject.advance(SHORT_PAUSE)

    released = await shop.subject.run(job)

    assert counts(held)["held"] == 1 and "held" not in counts(released)


async def test_a_stack_run_names_its_failure_reason_not_the_class_of_the_error(
    swarm_shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = swarm_shop
    shop.move()

    async def refuse(self: Any, *arguments: Any, **keywords: Any) -> Any:
        raise StackRejected({"stack": "x"}, "unresolved")

    monkeypatch.setattr(SwarmStackPlanner, "_pins", refuse)
    _, job = await shop.subject.rule(STACK, UPDATE)

    result = await shop.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "unresolved")


async def test_a_stack_preview_counts_the_services_the_viewer_can_read(swarm_shop: Shop) -> None:
    shop = swarm_shop
    subsystem = shop.subject.subsystem
    text = f'{{"kind":"stack","names":["{NAME}"]}}'

    seen = await subsystem.preview_targets(ADMIN, ENV_A, text)
    hidden = await subsystem.preview_targets(STANDARD, ENV_A, text)

    assert seen is not None and (seen.names, seen.note) == ((NAME,), "2 services")
    assert hidden is not None and hidden.names == ()


async def test_a_stack_preview_counts_only_the_services_the_viewer_may_read(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    controls = shop.rig.access.controls
    readable = ControlSpec(access_level="read", is_public=True)
    stack = ResourceKey(ENV_A, "stack", NAME)
    service = ResourceKey(ENV_A, "service", shop.member_id(API))
    await controls.update(
        ADMIN, stack, readable, expected_digest=await digest_of(shop.rig.access, stack)
    )
    await controls.create(
        ADMIN,
        service,
        ControlSpec(access_level="write", is_administrators_only=True),
        expected_digest=await digest_of(shop.rig.access, service),
    )
    text = f'{{"kind":"stack","names":["{NAME}"]}}'

    standard = await shop.subject.subsystem.preview_targets(STANDARD, ENV_A, text)
    administrator = await shop.subject.subsystem.preview_targets(ADMIN, ENV_A, text)

    assert standard is not None and standard.note == "1 service"
    assert administrator is not None and administrator.note == "2 services"


async def test_a_stack_name_no_row_knows_is_target_missing(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    _, job = await shop.subject.rule({"kind": "stack", "names": ["ghost"]}, UPDATE, name="ghost")
    before = shop.sent()

    result = await shop.subject.run(job)

    assert result.reason == "target_missing"
    assert shop.sent() == before and rows(shop.subject) == []


async def test_a_stack_among_several_names_is_redeployed_and_a_missing_one_is_reported(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    selector = {"kind": "stack", "names": ["ghost", NAME]}
    _, job = await shop.subject.rule(selector, UPDATE, name="several")

    result = await shop.subject.run(job)

    detail = counts(result)
    assert (result.outcome, detail["updated"]) == ("success", 1)
    assert (detail["targets"], detail["target_outcomes"]) == (NAME, f"{NAME}: done")
    assert (detail["not_found"], detail["not_found_names"]) == (1, "ghost")
    assert len(rows(shop.subject, "stack_redeployed")) == 1


async def test_a_stack_that_needs_a_redeploy_fails_the_run_when_compose_is_not_available(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    shop.rig.engine.compose = None
    _, job = await shop.subject.rule(STACK, UPDATE)
    before = shop.sent()

    result = await shop.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "compose_unavailable")
    assert shop.sent() == before and rows(shop.subject) == []


async def test_a_webhook_update_redeploys_the_stack_under_the_receiver(swarm_shop: Shop) -> None:
    shop = swarm_shop
    as_webhook(shop)
    shop.move()
    rule_id, job = await shop.subject.rule(STACK, UPDATE)

    result = await shop.subject.run(job, trigger="webhook")

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    [row] = rows(shop.subject, "stack_redeployed")
    assert (row["actor_subsystem"], row["rule_id"], row["target_id"]) == (RECEIVER, rule_id, NAME)
    assert (row["detail"]["trigger"], row["detail"]["webhook_action"]) == ("webhook", "update")


async def test_a_webhook_redeploy_sends_the_stack_without_a_registry_request(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    as_webhook(shop)
    rule_id, job = await shop.subject.rule(
        STACK, {"action": "redeploy", "verify_seconds": 0, "suppression_seconds": 0}
    )

    result = await shop.subject.run(job, trigger="webhook")

    assert (result.outcome, counts(result)["redeployed"]) == ("success", 1)
    assert shop.manifests.calls == []
    [row] = rows(shop.subject, "stack_redeployed")
    assert (row["actor_subsystem"], row["rule_id"]) == (RECEIVER, rule_id)
    assert (row["detail"]["trigger"], row["detail"]["webhook_action"]) == ("webhook", "redeploy")
    assert "references_updated" not in row["detail"]
