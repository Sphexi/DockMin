"""A rule runs with its owner's authority as the owner has it now."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.scheduler.model import RunResult
from app.services.object_locks import ObjectKey
from app.subsystems.base import RunContext, Subsystem, SubsystemDeps
from tests.scheduler.support import Rig, at, dispatch_of, scripted_subsystem

CONTAINER_ID = "a" * 64
IMAGE_ID = "b" * 64
FUTURE = "2030-01-01T00:00:00Z"
TERMINAL_REASONS = {
    "owner_access_lost": "The automation's owner no longer has access to run it.",
    "parameters_invalid": "The automation's parameters are not valid.",
}


class Calls:
    """Counts how often the run body was reached."""

    def __init__(self) -> None:
        self.count = 0

    @property
    def body(self) -> Callable[[Subsystem, RunContext], Awaitable[RunResult]]:
        calls = self

        async def run(subsystem: Subsystem, context: RunContext) -> RunResult:
            calls.count += 1
            return RunResult("success")

        return run


async def run_for(
    rig: Rig,
    deps: SubsystemDeps,
    calls: Calls,
    *,
    owner: int | None,
    environment_id: int | None = None,
    parameters: str = "{}",
    trigger: str = "schedule",
    enabled: bool = True,
    **attributes: Any,
) -> RunResult:
    rule = await rig.rule(
        f"rule-{len(rig.sql('SELECT id FROM automation_rule'))}",
        owner=owner,
        environment_id=environment_id,
        parameters=parameters,
        enabled=enabled,
    )
    job = await rig.job(rule_id=rule, next_run=at(60))
    subsystem = scripted_subsystem(calls.body, **attributes)(deps)
    return await dispatch_of(rig, subsystem, job, trigger=trigger)


async def test_an_enabled_administrators_rule_runs(rig: Rig, deps: SubsystemDeps) -> None:
    calls = Calls()

    ran = await run_for(rig, deps, calls, owner=rig.user("admin"))

    assert ran.outcome == "success" and calls.count == 1


@pytest.mark.parametrize(
    ("owner_kind", "reason"),
    [
        pytest.param("disabled-administrator", "owner_access_lost", id="disabled-administrator"),
        pytest.param(
            "standard-without-environment", "owner_access_lost", id="standard-with-no-host"
        ),
        pytest.param("no-owner", "owner_access_lost", id="owner-deleted"),
    ],
)
async def test_a_rule_whose_owner_has_lost_access_fails_without_running(
    rig: Rig, deps: SubsystemDeps, owner_kind: str, reason: str
) -> None:
    owners = {
        "disabled-administrator": rig.user("gone", enabled=False),
        "standard-without-environment": rig.user("standard", "standard"),
        "no-owner": None,
    }
    calls = Calls()

    ran = await run_for(rig, deps, calls, owner=owners[owner_kind])

    assert (ran.outcome, ran.reason, ran.message) == ("failure", reason, TERMINAL_REASONS[reason])
    assert calls.count == 0


async def test_a_rule_with_parameters_the_model_rejects_fails_without_running(
    rig: Rig, deps: SubsystemDeps
) -> None:
    calls = Calls()

    ran = await run_for(rig, deps, calls, owner=rig.user("admin"), parameters='{"surprise": 1}')

    assert (ran.outcome, ran.reason) == ("failure", "parameters_invalid")
    assert ran.message == TERMINAL_REASONS["parameters_invalid"]
    assert calls.count == 0


async def test_a_job_whose_rule_is_gone_fails_without_running(
    rig: Rig, deps: SubsystemDeps
) -> None:
    calls = Calls()
    job = await rig.job(rule_id=None, next_run=at(60))
    subsystem = scripted_subsystem(calls.body)(deps)

    ran = await dispatch_of(rig, subsystem, job)

    assert (ran.outcome, ran.reason) == ("failure", "rule_missing")
    assert calls.count == 0


async def test_a_subsystem_that_needs_a_host_refuses_a_rule_that_has_none(
    rig: Rig, deps: SubsystemDeps
) -> None:
    calls = Calls()

    ran = await run_for(rig, deps, calls, owner=rig.user("admin"), requires_environment=True)

    assert (ran.outcome, ran.reason) == ("failure", "environment_missing")
    assert calls.count == 0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        pytest.param("UPDATE automation_rule SET is_enabled = 0", "rule_disabled", id="disabled"),
        pytest.param(
            f"UPDATE automation_rule SET paused_until = '{FUTURE}'", "rule_paused", id="paused"
        ),
    ],
)
async def test_a_scheduled_run_of_a_disabled_or_paused_rule_is_skipped_but_a_manual_run_goes_on(
    rig: Rig, deps: SubsystemDeps, change: str, reason: str
) -> None:
    calls = Calls()
    admin = rig.user("admin")
    rule = await rig.rule("r", owner=admin)
    job = await rig.job(rule_id=rule, next_run=at(60))
    rig.sql(change)
    subsystem = scripted_subsystem(calls.body)(deps)

    scheduled = await dispatch_of(rig, subsystem, job)
    manual = await dispatch_of(rig, subsystem, job, trigger="manual")

    assert (scheduled.outcome, scheduled.reason) == ("skipped", reason)
    assert manual.outcome == "success"
    assert calls.count == 1


class StandardOwner:
    """A standard user owning a rule on a remote host, with a grant to change."""

    def __init__(self, rig: Rig, user: int, remote: int, local: int) -> None:
        self.rig = rig
        self.user = user
        self.remote = remote
        self.local = local

    def grant(self, environment: int) -> None:
        self.rig.sql(
            "INSERT INTO environment_user_access (environment_id, user_id) VALUES (?, ?)",
            (environment, self.user),
        )

    def revoke(self, environment: int) -> None:
        self.rig.sql("DELETE FROM environment_user_access WHERE environment_id = ?", (environment,))


@pytest.fixture
def standard(rig: Rig) -> StandardOwner:
    return StandardOwner(
        rig,
        rig.user("standard", "standard"),
        rig.environment("remote", kind="tcp_tls"),
        rig.environment("local", kind="unix_socket"),
    )


async def test_a_standard_owner_with_a_grant_on_a_remote_host_can_run(
    rig: Rig, deps: SubsystemDeps, standard: StandardOwner
) -> None:
    standard.grant(standard.remote)
    calls = Calls()

    ran = await run_for(
        rig,
        deps,
        calls,
        owner=standard.user,
        environment_id=standard.remote,
        requires_environment=True,
    )

    assert ran.outcome == "success" and calls.count == 1


async def test_a_standard_owner_never_runs_on_a_local_socket_even_with_a_grant(
    rig: Rig, deps: SubsystemDeps, standard: StandardOwner
) -> None:
    standard.grant(standard.local)
    calls = Calls()

    ran = await run_for(
        rig,
        deps,
        calls,
        owner=standard.user,
        environment_id=standard.local,
        requires_environment=True,
    )

    assert (ran.outcome, ran.reason) == ("failure", "owner_access_lost")
    assert calls.count == 0


async def test_a_standard_owners_grant_that_is_revoked_stops_the_next_run(
    rig: Rig, deps: SubsystemDeps, standard: StandardOwner
) -> None:
    standard.grant(standard.remote)
    calls = Calls()
    rule = await rig.rule("r", owner=standard.user, environment_id=standard.remote)
    job = await rig.job(rule_id=rule, next_run=at(60))
    subsystem = scripted_subsystem(calls.body, requires_environment=True)(deps)
    before = await dispatch_of(rig, subsystem, job)

    standard.revoke(standard.remote)
    after = await dispatch_of(rig, subsystem, job)

    assert (before.outcome, after.outcome, after.reason) == (
        "success",
        "failure",
        "owner_access_lost",
    )
    assert calls.count == 1


async def test_a_subsystems_own_target_check_is_left_to_the_run_so_each_name_is_settled_there(
    rig: Rig, deps: SubsystemDeps, standard: StandardOwner
) -> None:
    async def refuses(
        self: Subsystem,
        principal: object,
        environment_id: object,
        selector: object,
        authoriser: object,
    ) -> None:
        raise PermissionDenied("insufficient access")

    standard.grant(standard.remote)
    calls = Calls()

    ran = await run_for(
        rig,
        deps,
        calls,
        owner=standard.user,
        environment_id=standard.remote,
        requires_environment=True,
        authorise=refuses,
    )

    assert ran.outcome == "success" and calls.count == 1


async def test_an_administrator_owner_may_write_to_any_object(
    rig: Rig, deps: SubsystemDeps
) -> None:
    allowed: list[str] = []

    async def asks(self: Subsystem, context: RunContext) -> RunResult:
        for key in (ObjectKey(1, "container", CONTAINER_ID), ObjectKey(1, "image", IMAGE_ID)):
            await context.authorise_object(key)
            allowed.append(key.object_type)
        return RunResult("success")

    rule = await rig.rule("a", owner=rig.user("admin"))
    job = await rig.job(rule_id=rule, next_run=at(60))

    await dispatch_of(rig, scripted_subsystem(asks)(deps), job)

    assert allowed == ["container", "image"]


async def test_the_owner_is_read_again_when_the_run_asks_about_an_object(
    rig: Rig, deps: SubsystemDeps
) -> None:
    admin = rig.user("admin")
    refused: list[bool] = []

    async def owner_disabled_midway(self: Subsystem, context: RunContext) -> RunResult:
        rig.sql("UPDATE user SET is_enabled = 0 WHERE id = ?", (admin,))
        try:
            await context.authorise_object(ObjectKey(1, "container", CONTAINER_ID))
        except PermissionDenied:
            refused.append(True)
        return RunResult("success")

    rule = await rig.rule("a", owner=admin)
    job = await rig.job(rule_id=rule, next_run=at(60))

    await dispatch_of(rig, scripted_subsystem(owner_disabled_midway)(deps), job)

    assert refused == [True]


async def test_a_standard_owner_is_refused_an_object_type_the_access_model_does_not_cover(
    rig: Rig, deps: SubsystemDeps, standard: StandardOwner
) -> None:
    refused: list[bool] = []

    async def asks_about_an_image(self: Subsystem, context: RunContext) -> RunResult:
        try:
            await context.authorise_object(ObjectKey(standard.remote, "image", IMAGE_ID))
        except PermissionDenied:
            refused.append(True)
        return RunResult("success")

    standard.grant(standard.remote)
    rule = await rig.rule("s", owner=standard.user, environment_id=standard.remote)
    job = await rig.job(rule_id=rule, next_run=at(60))
    subsystem = scripted_subsystem(asks_about_an_image, requires_environment=True)(deps)

    await dispatch_of(rig, subsystem, job)

    assert refused == [True]


async def test_the_run_context_offers_no_owner_to_act_as(rig: Rig, deps: SubsystemDeps) -> None:
    seen: list[bool] = []

    async def inspects(self: Subsystem, context: RunContext) -> RunResult:
        seen.append(hasattr(context, "owner"))
        return RunResult("success")

    rule = await rig.rule("a", owner=rig.user("admin"))
    job = await rig.job(rule_id=rule, next_run=at(60))

    await dispatch_of(rig, scripted_subsystem(inspects)(deps), job)

    assert seen == [False]


async def test_a_rule_less_run_has_no_owner_to_authorise_against(
    rig: Rig, deps: SubsystemDeps
) -> None:
    async def asks(self: Subsystem, context: RunContext) -> RunResult:
        await context.authorise_object(ObjectKey(1, "container", CONTAINER_ID))
        return RunResult("success")

    job = await rig.refresher_job(next_run=at(60))
    subsystem = scripted_subsystem(asks, name="environment-snapshot-refresher")(deps)

    with pytest.raises(RuntimeError, match="no rule owner"):
        await dispatch_of(rig, subsystem, job)
