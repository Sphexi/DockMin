"""container-updater on a Swarm service: detection, update, rollback, refusal, skips and holds."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

import app.subsystems.container_updater as updater
from app.registry_client.manifests import ManifestCheckKind
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.image_bindings import BindingPullCredentials, SetCandidates
from app.services.image_updates import ImageUpdateService
from app.services.object_locks import LockHolder, ObjectKey
from app.services.resource_controls import ControlSpec
from app.services.swarm_services import SwarmServiceActions
from app.subsystems.container_updater import ContainerUpdater
from tests.access_support import ADMIN, ENV_A, STANDARD, digest_of
from tests.automation_support import Subject, counts
from tests.docker_engine.swarm_engine import plain_spec
from tests.image_support import (
    REFERENCE,
    decode_auth,
    digest,
    mark_missing,
    move_sets_elsewhere,
    register,
)
from tests.registry_support import ADDRESS, grant_user
from tests.swarm_updater_support import (
    REPORT,
    SERVICE,
    UPDATE,
    Built,
    after_update,
    compact,
    crashed,
    inject_sleeper,
    interrupted,
    paused,
    rows,
    still_updating,
    swarm_rolled_back,
)
from tests.updater_support import RECEIVER, UPDATER, as_webhook

STANDARD_ID = STANDARD.user_id
OLD_IMAGE = f"{REFERENCE}@{digest(1)}"
NEW_IMAGE = f"{REFERENCE}@{digest(2)}"
BUDGET_RUNNING_OUT = timedelta(seconds=-1)
SUPPRESSED_UPDATE = {**UPDATE, "suppression_seconds": 300}
SECOND_PASS_SECONDS = 61
MOUNTS = [{"Type": "bind", "Source": "/etc", "Target": "/x"}]


async def test_a_moved_digest_is_sent_to_the_service_under_the_rule_with_the_bound_sets_header(
    swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any, Any]] = []
    resolve = BindingPullCredentials.resolve

    async def spy(
        self: Any, environment_id: int, reference: str, *, principal: Any, choice: Any = None
    ) -> Any:
        calls.append((reference, principal, choice))
        return await resolve(self, environment_id, reference, principal=principal, choice=choice)

    monkeypatch.setattr(BindingPullCredentials, "resolve", spy)
    rule_id, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    assert swarm_web.image() == NEW_IMAGE
    [row] = rows(swarm_web.subject, "service_updated")
    assert (row["actor_type"], row["actor_subsystem"], row["rule_id"]) == (
        "subsystem",
        UPDATER,
        rule_id,
    )
    assert row["detail"]["trigger"] == "update" and row["detail"]["digest_after"] == digest(2)
    [(_, header)] = [h for h in swarm_web.host.auth_headers if h[0] == "update"]
    assert decode_auth(header or "")["username"] == "user-main"
    assert swarm_web.host.registry_auth_from[-1] is None
    assert swarm_web.host.mutating() == [f"POST /services/{swarm_web.sid}/update"]
    assert rows(swarm_web.subject, "image_pulled") == []
    assert [(p, c) for _, p, c in calls] == [(None, None)]


async def test_a_paused_update_is_rolled_back_with_both_rows_attributed_and_then_held(
    swarm_web: Built,
) -> None:
    after_update(swarm_web.host, paused)
    rule_id, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    first = await swarm_web.subject.run(job)

    detail = counts(first)
    assert (first.outcome, first.reason, detail["rolled_back"]) == (
        "failure",
        "update_paused",
        1,
    )
    [updated] = rows(swarm_web.subject, "service_updated")
    [back] = rows(swarm_web.subject, "service_rolled_back")
    assert (updated["result"], updated["detail"]["reason"]) == ("failure", "update_paused")
    assert updated["detail"]["verify_failure"] == "update_paused"
    assert back["result"] == "success" and back["actor_subsystem"] == UPDATER
    assert back["detail"]["failed_digest"] == digest(2)
    assert back["detail"]["rollback_reason"] == "update_paused"
    assert back["rule_id"] == updated["rule_id"] == rule_id
    assert swarm_web.image() == OLD_IMAGE
    written = len(rows(swarm_web.subject))

    again = await swarm_web.subject.run(job)

    assert counts(again)["held"] == 1 and len(rows(swarm_web.subject)) == written


async def test_a_new_registry_digest_after_a_rollback_is_a_new_attempt(swarm_web: Built) -> None:
    after_update(swarm_web.host, paused)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    await swarm_web.subject.run(job)
    swarm_web.manifests.default = digest(3)
    swarm_web.service.pop("UpdateStatus", None)
    swarm_web.subject.advance(SECOND_PASS_SECONDS)

    third = await swarm_web.subject.run(job)

    assert "held" not in counts(third)
    assert len(rows(swarm_web.subject, "service_updated")) == 2


async def test_a_service_swarm_rolled_back_itself_is_recorded_not_rolled_back_again(
    swarm_web: Built,
) -> None:
    after_update(swarm_web.host, swarm_rolled_back)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "rolled_back_by_swarm")
    assert "rolled_back" not in counts(result)
    assert rows(swarm_web.subject, "service_rolled_back") == []
    [updated] = rows(swarm_web.subject, "service_updated")
    assert updated["detail"]["reason"] == "rolled_back_by_swarm"
    swarm_web.set_image(OLD_IMAGE)
    swarm_web.service["Version"]["Index"] += 1

    again = await swarm_web.subject.run(job)

    assert counts(again)["held"] == 1


async def test_a_completed_rollout_that_left_the_tasks_down_is_rolled_back(
    swarm_web: Built,
) -> None:
    after_update(swarm_web.host, crashed)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "tasks_not_running")
    [updated] = rows(swarm_web.subject, "service_updated")
    [back] = rows(swarm_web.subject, "service_rolled_back")
    assert updated["detail"]["reason"] == "tasks_not_running"
    assert back["detail"]["rollback_reason"] == "tasks_not_running"
    assert swarm_web.image() == OLD_IMAGE


async def test_the_automated_rollback_names_the_image_it_undoes_and_the_one_it_restores(
    swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_update(swarm_web.host, paused)
    seen: list[dict[str, Any]] = []
    original = SwarmServiceActions.rollback

    async def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SwarmServiceActions, "rollback", spy)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert counts(result)["rolled_back"] == 1
    assert seen[0]["expected_image"] == NEW_IMAGE
    assert seen[0]["expected_previous_image"] == OLD_IMAGE


async def test_a_rollout_that_outlasts_the_watch_and_is_then_rolled_back_by_swarm_is_held(
    swarm_web: Built,
) -> None:
    after_update(swarm_web.host, still_updating)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    first = await swarm_web.subject.run(job)
    [row] = rows(swarm_web.subject, "service_updated")
    service = swarm_web.service
    service["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"] = OLD_IMAGE
    service["UpdateStatus"] = {
        "State": "rollback_completed",
        "StartedAt": "2026-06-01T00:00:00Z",
        "CompletedAt": "2026-06-01T00:05:00Z",
    }
    service["Version"]["Index"] += 1
    swarm_web.subject.advance(SECOND_PASS_SECONDS)

    again = await swarm_web.subject.run(job)

    assert (first.outcome, row["detail"]["verify_failure"]) == ("success", "still_updating")
    assert counts(again)["held"] == 1
    assert len(rows(swarm_web.subject, "service_updated")) == 1
    swarm_web.manifests.default = digest(3)
    swarm_web.subject.advance(SECOND_PASS_SECONDS)
    assert "held" not in counts(await swarm_web.subject.run(job))


async def test_a_cancelled_watch_leaves_a_failure_row_under_the_rule(
    swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    inject_sleeper(monkeypatch, interrupted)
    after_update(
        swarm_web.host, lambda host, service: service.update(UpdateStatus={"State": "updating"})
    )
    rule_id, job = await swarm_web.subject.rule(SERVICE, {**UPDATE, "verify_seconds": 4})

    with pytest.raises(asyncio.CancelledError):
        await swarm_web.subject.run(job)

    [row] = rows(swarm_web.subject, "service_updated")
    assert (row["result"], row["rule_id"], row["actor_subsystem"]) == ("failure", rule_id, UPDATER)
    assert (row["detail"]["reason"], row["detail"]["interrupted"]) == ("interrupted", True)


async def test_the_runs_reason_is_the_errors_own_reason(swarm_web: Built) -> None:
    after_update(swarm_web.host, paused)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "update_paused")


async def test_a_binding_that_does_not_fit_is_refused_once_held_and_no_other_set_is_asked(
    swarm: Any, swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(swarm, "other")
    await move_sets_elsewhere(swarm.db)
    calls: list[tuple[str, Any, Any]] = []
    candidate_reads: list[Any] = []
    decide, candidates = BindingPullCredentials.decide, SetCandidates.at

    async def spy(
        self: Any, environment_id: int, reference: str, *, principal: Any, choice: Any = None
    ) -> Any:
        calls.append((reference, principal, choice))
        return await decide(self, environment_id, reference, principal=principal, choice=choice)

    async def read(self: Any, *arguments: Any, **keywords: Any) -> Any:
        candidate_reads.append(arguments)
        return await candidates(self, *arguments, **keywords)

    monkeypatch.setattr(BindingPullCredentials, "decide", spy)
    monkeypatch.setattr(SetCandidates, "at", read)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    first = await swarm_web.subject.run(job)
    swarm_web.subject.advance(SECOND_PASS_SECONDS)
    again = await swarm_web.subject.run(job)

    [row] = rows(swarm_web.subject, "image_update_refused")
    assert (first.outcome, first.reason) == ("failure", "credential_mismatch")
    assert (row["target_type"], row["target_id"]) == ("service", swarm_web.sid)
    assert row["detail"]["reason"] == "credential_mismatch"
    assert rows(swarm_web.subject, "service_updated") == []
    assert swarm_web.image() == OLD_IMAGE and swarm_web.host.mutating() == []
    assert calls == [] and candidate_reads == []
    assert swarm_web.host.auth_headers == []
    assert counts(again)["held"] == 1 and len(rows(swarm_web.subject, "image_update_refused")) == 1


async def test_a_set_moved_between_the_check_and_the_act_is_refused_without_trying_another(
    swarm: Any, swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(swarm, "other")
    check = ImageUpdateService.check
    calls: list[tuple[str, Any, Any]] = []
    resolve = BindingPullCredentials.resolve

    async def racing(self: Any, *arguments: Any, **keywords: Any) -> Any:
        found = await check(self, *arguments, **keywords)
        await move_sets_elsewhere(swarm.db)
        return found

    async def spy(
        self: Any, environment_id: int, reference: str, *, principal: Any, choice: Any = None
    ) -> Any:
        calls.append((reference, principal, choice))
        return await resolve(self, environment_id, reference, principal=principal, choice=choice)

    monkeypatch.setattr(ImageUpdateService, "check", racing)
    monkeypatch.setattr(BindingPullCredentials, "resolve", spy)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert result.reason == "credential_unavailable"
    assert calls == [(REFERENCE, None, None)]
    [row] = rows(swarm_web.subject, "image_update_refused")
    assert row["detail"]["reason"] == "credential_unavailable"
    assert swarm_web.image() == OLD_IMAGE


async def test_a_service_that_needs_authentication_is_refused_once_and_then_held(
    swarm: Any, swarm_web: Built
) -> None:
    await swarm.db.execute("DELETE FROM image_credential_binding")
    swarm_web.manifests.default = ManifestCheckKind.AUTHENTICATION
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    first = await swarm_web.subject.run(job)
    swarm_web.subject.advance(10)
    held = await swarm_web.subject.run(job)

    assert (first.outcome, first.reason) == ("failure", "authentication_required")
    [row] = rows(swarm_web.subject, "image_update_refused")
    assert row["detail"]["reason"] == "authentication_required"
    assert (held.outcome, counts(held)["held"]) == ("success", 1)
    assert len(rows(swarm_web.subject, "image_update_refused")) == 1
    assert swarm_web.host.mutating() == []


async def test_a_service_whose_bound_set_was_deleted_is_updated_anonymously_and_counted(
    swarm: Any, swarm_web: Built
) -> None:
    await mark_missing(swarm.db, swarm_web.set_id)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert result.outcome == "success"
    assert counts(result)["anonymous_fallback"] == 1
    assert rows(swarm_web.subject, "image_update_refused") == []
    assert len(rows(swarm_web.subject, "service_updated")) == 1
    [(_, header)] = [h for h in swarm_web.host.auth_headers if h[0] == "update"]
    assert decode_auth(header or "") == {}


def make_unpinned(built: Built) -> None:
    built.set_image(REFERENCE)


def make_pinned(built: Built) -> None:
    built.set_image(f"{ADDRESS}/app@{digest(1)}")


def make_image_id(built: Built) -> None:
    built.set_image("a" * 12)


def make_updating(built: Built) -> None:
    built.service["UpdateStatus"] = {"State": "updating"}


def make_paused(built: Built) -> None:
    built.service["UpdateStatus"] = {"State": "paused"}


def make_scaled_to_zero(built: Built) -> None:
    built.spec["Mode"] = {"Replicated": {"Replicas": 0}}


def make_a_job(built: Built) -> None:
    built.spec["Mode"] = {"ReplicatedJob": {}}


SKIPS = [
    ("unpinned", make_unpinned, "unpinned"),
    ("pinned", make_pinned, "pinned"),
    ("image-id", make_image_id, "image_id_reference"),
    ("updating", make_updating, "update_in_progress"),
    ("paused", make_paused, "update_paused"),
    ("scaled-to-zero", make_scaled_to_zero, "not_running"),
    ("job", make_a_job, "job_mode"),
]


@pytest.mark.parametrize(
    ("change", "skip"), [row[1:] for row in SKIPS], ids=[row[0] for row in SKIPS]
)
async def test_a_service_the_updater_must_not_touch_is_counted_under_its_reason_and_left_alone(
    swarm_web: Built, change: Any, skip: str
) -> None:
    change(swarm_web)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, counts(result)[skip]) == ("success", 1)
    assert rows(swarm_web.subject) == [] and swarm_web.host.mutating() == []


async def test_a_service_in_a_different_environment_type_or_that_is_gone_is_target_missing(
    swarm: Any, swarm_web: Built
) -> None:
    _, gone = await swarm_web.subject.rule(
        {"kind": "service", "names": ["nope"]}, UPDATE, name="gone"
    )
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    missing = await swarm_web.subject.run(gone)
    await swarm.db.execute("UPDATE environment SET type = 'standalone' WHERE id = ?", (ENV_A,))

    standalone = await swarm_web.subject.run(job)

    assert missing.reason == "target_missing" and standalone.reason == "target_missing"
    assert swarm_web.host.mutating() == []


async def test_a_report_names_the_reference_and_a_dry_run_forecasts_without_a_lock_or_a_write(
    swarm: Any, swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    _, report = await swarm_web.subject.rule(SERVICE, REPORT, name="report")
    taken: list[Any] = []
    hold = swarm.engine.locks.hold

    def recording_hold(*a: Any, **k: Any) -> Any:
        taken.append(a)
        return hold(*a, **k)

    monkeypatch.setattr(swarm.engine.locks, "hold", recording_hold)

    dry = await swarm_web.subject.run(job, dry=True)
    reported = await swarm_web.subject.run(report)

    assert counts(dry)["would_update"] == "service web" and counts(dry)["would_update_count"] == 1
    assert counts(reported)["update_available"] == 1
    assert counts(reported)["update_available_refs"] == REFERENCE
    assert swarm_web.host.mutating() == [] and rows(swarm_web.subject) == [] and taken == []
    assert swarm_web.manifests.calls and swarm_web.image() == OLD_IMAGE


async def test_the_start_budget_defers_an_update_it_has_no_time_for(
    swarm_web: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updater, "START_BUDGET", BUDGET_RUNNING_OUT)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job)

    assert counts(result)["deferred"] == 1
    assert rows(swarm_web.subject) == [] and swarm_web.host.mutating() == []


async def test_an_owner_whose_restriction_the_spec_breaks_is_refused_once_and_then_held(
    swarm: Any, swarm_web: Built
) -> None:
    swarm_web.spec["TaskTemplate"]["ContainerSpec"]["Mounts"] = MOUNTS
    service = ResourceKey(ENV_A, "service", swarm_web.sid)
    await swarm.access.controls.create(
        ADMIN,
        service,
        ControlSpec(access_level="write", owner_user_id=STANDARD_ID),
        expected_digest=await digest_of(swarm.access, service),
    )
    await grant_user(swarm.db, swarm_web.set_id, STANDARD_ID)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE, owner=STANDARD_ID)

    first = await swarm_web.subject.run(job)
    again = await swarm_web.subject.run(job)

    [row] = rows(swarm_web.subject, "service_updated")
    assert first.outcome == "failure" and row["detail"]["reason"] == "restricted"
    assert counts(again)["held"] == 1 and swarm_web.host.mutating() == []


async def test_a_service_a_user_just_acted_on_is_suppressed_and_the_run_is_skipped(
    swarm: Any, swarm_web: Built
) -> None:
    _, job = await swarm_web.subject.rule(SERVICE, SUPPRESSED_UPDATE)
    key = ObjectKey(ENV_A, "service", swarm_web.sid)
    async with swarm.engine.locks.hold(key, LockHolder(UserActor.of(ADMIN), "admin", "scale")):
        pass

    result = await swarm_web.subject.run(job)

    assert result.outcome == "skipped"
    assert rows(swarm_web.subject) == [] and swarm_web.host.mutating() == []


async def test_a_service_another_user_holds_ends_the_run_skipped_without_a_row(
    swarm: Any, swarm_web: Built
) -> None:
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    swarm_web.subject.subsystem.lock_wait = 0.01  # type: ignore[misc]
    key = ObjectKey(ENV_A, "service", swarm_web.sid)

    async with swarm.engine.locks.hold(key, LockHolder(UserActor.of(ADMIN), "admin", "scale")):
        result = await swarm_web.subject.run(job)

    assert result.outcome == "skipped"
    assert rows(swarm_web.subject) == [] and swarm_web.host.mutating() == []


async def test_a_service_the_owner_cannot_see_fails_the_run_as_missing_and_nothing_is_sent(
    swarm_web: Built,
) -> None:
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE, owner=STANDARD_ID)

    result = await swarm_web.subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert swarm_web.host.mutating() == [] and compact(swarm_web.subject) == []


async def test_several_named_services_are_each_updated_and_a_missing_name_is_reported(
    swarm_web: Built,
) -> None:
    swarm_web.host.add_service(plain_spec("api", image=OLD_IMAGE, replicas=1))
    _, job = await swarm_web.subject.rule(
        {"kind": "service", "names": ["web", "ghost", "api"]}, UPDATE
    )

    result = await swarm_web.subject.run(job)

    detail = counts(result)
    assert (result.outcome, detail["updated"]) == ("success", 2)
    assert (detail["targets"], detail["target_outcomes"]) == (
        "api, web",
        "api: done, web: done",
    )
    assert (detail["not_found"], detail["not_found_names"]) == (1, "ghost")
    assert len(rows(swarm_web.subject, "service_updated")) == 2


async def test_a_service_the_owner_cannot_see_among_several_is_reported_as_not_found(
    swarm: Any, swarm_web: Built
) -> None:
    api = swarm_web.host.add_service(plain_spec("api", image=OLD_IMAGE, replicas=1))
    service = ResourceKey(ENV_A, "service", api)
    await swarm.access.controls.create(
        ADMIN,
        service,
        ControlSpec(access_level="write", owner_user_id=STANDARD_ID),
        expected_digest=await digest_of(swarm.access, service),
    )
    await grant_user(swarm.db, swarm_web.set_id, STANDARD_ID)
    _, job = await swarm_web.subject.rule(
        {"kind": "service", "names": ["api", "web"]}, UPDATE, owner=STANDARD_ID
    )

    result = await swarm_web.subject.run(job)

    detail = counts(result)
    assert (result.outcome, detail["updated"]) == ("success", 1)
    assert detail["targets"] == "api"
    assert (detail["not_found"], detail["not_found_names"]) == (1, "web")
    assert swarm_web.image() == OLD_IMAGE


async def test_no_row_the_service_path_writes_names_the_scheduler(swarm_web: Built) -> None:
    after_update(swarm_web.host, paused)
    _, job = await swarm_web.subject.rule(SERVICE, UPDATE)
    await swarm_web.subject.run(job)
    await swarm_web.subject.run(job)

    actors = {r["actor_subsystem"] for r in swarm_web.subject.events()}

    assert actors == {UPDATER}


async def test_a_service_preview_names_the_service_to_a_viewer_who_can_read_it_and_none_other(
    swarm_web: Built,
) -> None:
    subsystem = swarm_web.subject.subsystem
    text = '{"kind":"service","names":["web"]}'

    seen = await subsystem.preview_targets(ADMIN, ENV_A, text)
    hidden = await subsystem.preview_targets(STANDARD, ENV_A, text)
    absent = await subsystem.preview_targets(ADMIN, ENV_A, '{"kind":"service","names":["nope"]}')

    assert seen is not None and (seen.names, seen.note) == (("web",), "2 replicas")
    assert hidden is not None and hidden.names == ()
    assert absent is not None and absent.names == ()


async def test_an_updater_built_without_a_registry_runtime_fails_a_service_rules_runs(
    swarm: Any, settings: Any
) -> None:
    subject = Subject(swarm, settings, ContainerUpdater)
    _, job = await subject.rule(SERVICE, UPDATE)

    result = await subject.run(job)

    assert (result.outcome, result.reason) == ("failure", "registry_runtime_missing")


WEBHOOK_REDEPLOY = {"action": "redeploy", "verify_seconds": 0, "suppression_seconds": 0}


async def test_a_webhook_update_moves_the_service_and_attributes_the_row_to_the_receiver(
    swarm_web: Built,
) -> None:
    as_webhook(swarm_web)
    rule_id, job = await swarm_web.subject.rule(SERVICE, UPDATE)

    result = await swarm_web.subject.run(job, trigger="webhook")

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    assert swarm_web.image() == NEW_IMAGE
    [row] = rows(swarm_web.subject, "service_updated")
    assert (row["actor_subsystem"], row["rule_id"]) == (RECEIVER, rule_id)
    assert row["detail"]["trigger"] == "webhook" and row["detail"]["webhook_action"] == "update"


async def test_a_webhook_redeploy_force_updates_the_service_without_a_registry_request(
    swarm_web: Built,
) -> None:
    as_webhook(swarm_web)
    rule_id, job = await swarm_web.subject.rule(SERVICE, WEBHOOK_REDEPLOY)

    result = await swarm_web.subject.run(job, trigger="webhook")

    assert (result.outcome, counts(result)["redeployed"]) == ("success", 1)
    assert swarm_web.image() == OLD_IMAGE
    assert swarm_web.manifests.calls == []
    assert swarm_web.host.mutating() == [f"POST /services/{swarm_web.sid}/update"]
    [row] = rows(swarm_web.subject, "service_force_updated")
    assert (row["actor_subsystem"], row["rule_id"]) == (RECEIVER, rule_id)
    assert row["detail"]["trigger"] == "webhook" and row["detail"]["webhook_action"] == "redeploy"
