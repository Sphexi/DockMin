"""The subsystem base class: event policy, dry runs, result conversion and construction."""

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.config import Settings
from app.scheduler.model import RunFailed, RunResult, RunSkipped
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.subsystems.base import RuleParameters, RunContext, Subsystem, SubsystemDeps
from tests.scheduler.support import (
    REFRESHER,
    RESTARTER,
    Parameters,
    Rig,
    at,
    detail_of,
    dispatch_of,
    scripted_subsystem,
)

type Body = Callable[[Any, RunContext], Awaitable[RunResult]]

CONTAINER = ObjectKey(1, "container", "a" * 64)
TOO_LONG_A_WAIT = 61.0
RECORDED_ACTION = "did_something"


def reporting(result: RunResult, *, writes_event: bool) -> Body:
    async def run(self: Subsystem, context: RunContext) -> RunResult:
        if writes_event:
            await context.activity.record_event(
                context.actor,
                action=RECORDED_ACTION,
                target_type="container",
                target_id="x",
                result="success",
            )
        return result

    return run


async def refresher_run(rig: Rig, deps: SubsystemDeps, body: Body, **attributes: Any) -> RunResult:
    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(body, name=REFRESHER, **attributes)(deps)
    return await dispatch_of(rig, subsystem, job)


@pytest.mark.parametrize(
    ("policy", "result", "writes_event", "outcome", "reason"),
    [
        pytest.param(
            "per_action",
            RunResult("success", attempted=1),
            False,
            "failure",
            "no_activity_event",
            id="per-action-acted-without-an-event",
        ),
        pytest.param(
            "per_action",
            RunResult("success", attempted=1),
            True,
            "success",
            None,
            id="per-action-acted-with-an-event",
        ),
        pytest.param(
            "per_action",
            RunResult("success", attempted=0),
            False,
            "success",
            None,
            id="per-action-attempted-nothing",
        ),
        pytest.param(
            "none",
            RunResult("success", attempted=1),
            False,
            "success",
            None,
            id="none-never-requires-one",
        ),
        pytest.param(
            "failures_only",
            RunResult("success", attempted=3),
            False,
            "success",
            None,
            id="failures-only-success",
        ),
        pytest.param(
            "failures_only",
            RunResult("partial", attempted=2),
            False,
            "failure",
            "no_activity_event",
            id="failures-only-partial-without-an-event",
        ),
        pytest.param(
            "failures_only",
            RunResult("partial", attempted=2),
            True,
            "partial",
            None,
            id="failures-only-partial-with-an-event",
        ),
    ],
)
async def test_a_result_needs_an_activity_event_only_where_the_event_policy_requires_one(
    rig: Rig,
    deps: SubsystemDeps,
    policy: str,
    result: RunResult,
    writes_event: bool,
    outcome: str,
    reason: str | None,
) -> None:
    ran = await refresher_run(
        rig, deps, reporting(result, writes_event=writes_event), event_policy=policy
    )

    assert (ran.outcome, ran.reason) == (outcome, reason)


@pytest.mark.parametrize("development_checks", [True, False])
async def test_a_run_that_acts_without_an_event_fails_in_every_configuration_and_logs_it(
    rig: Rig, settings: Settings, development_checks: bool, logged: Any
) -> None:
    configured = settings.model_copy(update={"development_checks": development_checks})
    deps, _ = rig.deps(configured)

    ran = await refresher_run(
        rig,
        deps,
        reporting(RunResult("success", attempted=1), writes_event=False),
        event_policy="per_action",
    )

    assert (ran.outcome, ran.reason) == ("failure", "no_activity_event")
    assert [record["subsystem"] for record in logged("subsystem_event_missing")] == [REFRESHER]


async def test_an_event_a_run_writes_names_the_subsystem_and_the_run(
    rig: Rig, deps: SubsystemDeps
) -> None:
    await refresher_run(rig, deps, reporting(RunResult("success", attempted=1), writes_event=True))

    [event] = rig.events(RECORDED_ACTION)
    assert (event["actor_type"], event["actor_subsystem"], event["rule_id"]) == (
        "subsystem",
        REFRESHER,
        None,
    )
    assert event["job_run_id"] == rig.sql("SELECT max(id) AS id FROM job_run")[0]["id"]


async def test_a_dry_run_needs_no_event(rig: Rig, deps: SubsystemDeps) -> None:
    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(
        reporting(RunResult("success", attempted=1), writes_event=False),
        name=REFRESHER,
        supports_dry_run=True,
    )(deps)

    ran = await dispatch_of(rig, subsystem, job, trigger="dry_run", dry_run=True)

    assert ran.outcome == "success"


async def test_an_event_written_under_a_rule_carries_the_rules_id_and_name(
    rig: Rig, deps: SubsystemDeps
) -> None:
    rule = await rig.rule("Nightly restart")
    job = await rig.job(rule_id=rule, next_run=at(60))
    subsystem = scripted_subsystem(reporting(RunResult("success", attempted=1), writes_event=True))(
        deps
    )

    await dispatch_of(rig, subsystem, job)

    [event] = rig.events(RECORDED_ACTION)
    assert event["rule_id"] == rule
    assert detail_of(event) == {"rule_id": rule, "rule_name": "Nightly restart"}


def deleting_the_rule(rig: Rig, rule: int, result: RunResult, *, records_unlinked: bool) -> Body:
    async def run(self: Subsystem, context: RunContext) -> RunResult:
        rig.sql("DELETE FROM automation_rule WHERE id = ?", (rule,))
        if records_unlinked:
            async with context.activity.audit(
                context.actor, action=RECORDED_ACTION, target_type="container", target_id="x"
            ):
                pass
        return result

    return run


@pytest.mark.parametrize(
    ("records_unlinked", "outcome", "reason", "reported"),
    [
        pytest.param(False, "failure", "no_activity_event", 1, id="rule-deleted-and-no-event"),
        pytest.param(True, "success", None, 0, id="rule-deleted-and-an-unlinked-event"),
    ],
)
async def test_a_run_whose_rule_is_deleted_keeps_its_result_only_if_it_wrote_an_unlinked_event(
    rig: Rig,
    deps: SubsystemDeps,
    logged: Any,
    records_unlinked: bool,
    outcome: str,
    reason: str | None,
    reported: int,
) -> None:
    rule = await rig.rule("Nightly restart")
    job = await rig.job(rule_id=rule, next_run=at(60))
    body = deleting_the_rule(
        rig, rule, RunResult("success", attempted=1), records_unlinked=records_unlinked
    )
    subsystem = scripted_subsystem(body)(deps)

    ran = await dispatch_of(rig, subsystem, job)

    assert (ran.outcome, ran.reason) == (outcome, reason)
    assert len(logged("subsystem_event_missing")) == reported


async def raise_after_nothing(self: Subsystem, context: RunContext) -> RunResult:
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("error", "outcome", "reason", "message"),
    [
        pytest.param(
            RunSkipped("nothing_to_do", "Nothing matched.", {"count": 0}),
            "skipped",
            "nothing_to_do",
            "Nothing matched.",
            id="skip",
        ),
        pytest.param(
            RunFailed("bad_config", "The configuration is wrong."),
            "failure",
            "bad_config",
            "The configuration is wrong.",
            id="failure",
        ),
        pytest.param(
            ObjectBusy(LockHolder(UserActor(9), "alice", "stop")),
            "skipped",
            "object_busy",
            "The object was busy with another action.",
            id="busy",
        ),
    ],
)
async def test_a_signal_raised_by_a_run_becomes_its_result(
    rig: Rig, deps: SubsystemDeps, error: Exception, outcome: str, reason: str, message: str
) -> None:
    async def run(self: Subsystem, context: RunContext) -> RunResult:
        raise error

    ran = await refresher_run(rig, deps, run)

    assert (ran.outcome, ran.reason, ran.message) == (outcome, reason, message)


async def test_a_skip_keeps_the_detail_the_run_gave(rig: Rig, deps: SubsystemDeps) -> None:
    async def run(self: Subsystem, context: RunContext) -> RunResult:
        raise RunSkipped("nothing_to_do", "Nothing matched.", {"count": 0})

    ran = await refresher_run(rig, deps, run)

    assert dict(ran.detail) == {"count": 0}


@pytest.mark.parametrize(
    ("reason", "body"),
    [
        pytest.param("dry_run_took_lock", "lock", id="takes-a-lock"),
        pytest.param("dry_run_took_lock", "swallowed_lock", id="swallows-the-lock-refusal"),
        pytest.param("dry_run_wrote_event", "event", id="writes-an-event"),
        pytest.param(
            "dry_run_wrote_event", "swallowed_failure_event", id="swallows-the-event-refusal"
        ),
    ],
)
async def test_a_dry_run_that_takes_a_lock_or_writes_an_event_fails_and_writes_nothing(
    rig: Rig, deps: SubsystemDeps, reason: str, body: str
) -> None:
    async def takes_lock(self: Subsystem, context: RunContext) -> RunResult:
        async with context.hold(CONTAINER, "restart"):
            pass
        return RunResult("success")

    async def swallows_lock(self: Subsystem, context: RunContext) -> RunResult:
        try:
            async with context.locks.hold(CONTAINER, LockHolder(context.actor, "x", "restart")):
                pass
        except RunFailed:
            pass
        return RunResult("success")

    async def writes_event(self: Subsystem, context: RunContext) -> RunResult:
        await context.activity.record_event(
            context.actor,
            action=RECORDED_ACTION,
            target_type="container",
            target_id="x",
            result="success",
        )
        return RunResult("success", attempted=1)

    async def swallows_event_refusal(self: Subsystem, context: RunContext) -> RunResult:
        with contextlib.suppress(RunFailed):
            await context.activity.record_failure(
                context.actor, action=RECORDED_ACTION, target_type="container", reason="x"
            )
        return RunResult("success")

    bodies = {
        "lock": takes_lock,
        "swallowed_lock": swallows_lock,
        "event": writes_event,
        "swallowed_failure_event": swallows_event_refusal,
    }
    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(bodies[body], name=REFRESHER, supports_dry_run=True)(deps)

    ran = await dispatch_of(rig, subsystem, job, trigger="dry_run", dry_run=True)

    assert (ran.outcome, ran.reason) == ("failure", reason)
    assert rig.events() == []


async def test_a_dry_run_that_changes_nothing_succeeds(rig: Rig, deps: SubsystemDeps) -> None:
    async def clean(self: Subsystem, context: RunContext) -> RunResult:
        assert context.dry_run is True
        return RunResult("success")

    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(clean, name=REFRESHER, supports_dry_run=True)(deps)

    ran = await dispatch_of(rig, subsystem, job, trigger="dry_run", dry_run=True)

    assert ran.outcome == "success"


def test_a_subsystem_does_not_support_a_dry_run_unless_it_says_so() -> None:
    assert Subsystem.supports_dry_run is False


@pytest.mark.parametrize(
    ("name", "attributes"),
    [
        pytest.param(
            REFRESHER, {"parameters_model": Parameters}, id="parameters-on-a-rule-less-one"
        ),
        pytest.param(
            RESTARTER, {"parameters_model": None}, id="no-parameters-on-a-rule-bearing-one"
        ),
        pytest.param(REFRESHER, {"lock_wait": TOO_LONG_A_WAIT}, id="lock-wait-over-a-minute"),
        pytest.param(
            REFRESHER,
            {"overrun_policy": "queue", "allowed_overrun_policies": frozenset({"skip"})},
            id="default-policy-outside-the-allowed-set",
        ),
        pytest.param("scheduler", {}, id="name-outside-the-registry"),
    ],
)
def test_a_subsystem_that_contradicts_the_registry_or_its_own_limits_is_refused(
    deps: SubsystemDeps, name: str, attributes: dict[str, Any]
) -> None:
    namespace: dict[str, Any] = {"name": name, "run": raise_after_nothing} | attributes

    with pytest.raises(ValueError, match=r"registered|disagree|lock_wait|overrun_policy"):
        type("Bad", (Subsystem,), namespace)(deps)


def test_a_rule_bearing_subsystem_must_implement_authorise_target(deps: SubsystemDeps) -> None:
    class Permissive(Subsystem):
        name = RESTARTER
        parameters_model = RuleParameters

        async def run(self, context: RunContext) -> RunResult:
            return RunResult("success")

    with pytest.raises(TypeError, match="authorise_target"):
        Permissive(deps)


def test_a_rule_less_subsystem_needs_no_target_check(deps: SubsystemDeps) -> None:
    subsystem = scripted_subsystem(raise_after_nothing, name=REFRESHER)(deps)

    assert subsystem.records_events is True


def test_a_subsystem_with_the_none_policy_reports_that_it_records_no_events(
    deps: SubsystemDeps,
) -> None:
    subsystem = scripted_subsystem(raise_after_nothing, name=REFRESHER, event_policy="none")(deps)

    assert subsystem.records_events is False


async def test_the_default_target_check_refuses_everything(deps: SubsystemDeps) -> None:
    class Permissive(Subsystem):
        name = REFRESHER

        async def run(self, context: RunContext) -> RunResult:
            return RunResult("success")

    with pytest.raises(PermissionDenied):
        await Permissive(deps).authorise_target(
            Principal(1, "u", "administrator"),
            1,
            "",
            None,  # type: ignore[arg-type]
        )


def test_the_default_selector_parser_refuses_a_selector_and_accepts_an_empty_one(
    deps: SubsystemDeps,
) -> None:
    subsystem = scripted_subsystem(raise_after_nothing, name=REFRESHER)(deps)

    assert subsystem.parse_selector("   ") == ""
    with pytest.raises(ValidationFailed):
        subsystem.parse_selector("web")
