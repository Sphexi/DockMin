"""The per-object tally and the guarded act on one object that multi-object runs share."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ProductError, ResourceNotFound, SelfProtected
from app.scheduler.model import RunFailed, RunResult, RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.containers import ContainerListTooLarge
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_deploy import StackRejected
from app.services.swarm_service_views import ServiceUpdateFailed
from app.subsystems.acting_run import (
    ALL_OBJECTS_FAILED,
    PERMISSION_DENIED,
    SOME_OBJECTS_FAILED,
    ObjectStatus,
    ObjectTally,
    act_on,
    check_authorised,
    failure_reason,
    forecast_busy,
)
from app.subsystems.base import OwnerGate
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import Gate, run_context
from tests.scheduler.support import Rig

KEY = ObjectKey(ENV_A, "container", "a" * 64)
OTHER_KEY = ObjectKey(ENV_A, "container", "b" * 64)
DETAIL_TEXT_LIMIT = 256


def tally() -> ObjectTally:
    return ObjectTally(done_name="restarted", plan_name="would_restart")


def counted(*statuses: ObjectStatus) -> ObjectTally:
    built = tally()
    for status in statuses:
        built.record(status, attempted=status in ("done", "failed"), reason="engine_error")
    return built


def detail(result: RunResult) -> dict[str, Any]:
    return dict(result.detail)


@pytest.mark.parametrize(
    ("statuses", "outcome", "reason"),
    [
        ((), "success", None),
        (("done", "done"), "success", None),
        (("done", "held", "suppressed", "busy", "not_permitted", "deferred"), "success", None),
        (("done", "failed"), "partial", "engine_error"),
        (("exhausted", "done"), "partial", "engine_error"),
        (("done", "failed", "failed"), "partial", SOME_OBJECTS_FAILED),
        (("failed",), "failure", "engine_error"),
        (("exhausted",), "failure", "engine_error"),
        (("failed", "failed"), "failure", ALL_OBJECTS_FAILED),
        (("failed", "exhausted"), "failure", ALL_OBJECTS_FAILED),
        (("held", "failed"), "failure", "engine_error"),
    ],
)
def test_the_run_outcome_follows_what_succeeded_and_what_failed(
    statuses: tuple[ObjectStatus, ...], outcome: str, reason: str | None
) -> None:
    result = counted(*statuses).result(dry_run=False)

    assert (result.outcome, result.reason) == (outcome, reason)


def test_ten_objects_of_which_three_fail_make_a_partial_run_of_seven_and_three() -> None:
    built = tally()
    for _ in range(7):
        built.record("done", attempted=True)
    for _ in range(3):
        built.record("failed", attempted=True, reason="engine_error")

    result = built.result(dry_run=False)

    assert result.outcome == "partial"
    assert result.attempted == 10
    assert detail(result) == {"restarted": 7, "failed": 3}


def test_detail_holds_counts_only_for_statuses_that_occurred() -> None:
    result = counted("done", "held", "held", "busy", "deferred").result(dry_run=False)

    assert detail(result) == {"restarted": 1, "held": 2, "busy": 1, "deferred": 1}


def test_a_skipped_object_is_counted_under_its_reason_and_is_not_a_failure() -> None:
    built = tally()
    built.record("skipped", attempted=False, reason="not_running")
    built.record("skipped", attempted=False, reason="not_running")
    built.record("skipped", attempted=False, reason="swarm_managed")
    built.record("skipped", attempted=False)

    result = built.result(dry_run=False)

    assert result.outcome == "success"
    assert detail(result) == {"restarted": 0, "not_running": 2, "swarm_managed": 1, "skipped": 1}


def test_only_objects_a_service_was_called_for_count_as_attempted() -> None:
    built = tally()
    built.record("done", attempted=True)
    built.record("failed", attempted=False, reason="permission_denied")
    built.record("exhausted", attempted=True, reason="restart_budget_exhausted")

    assert built.result(dry_run=False).attempted == 2


def test_an_unusable_failure_reason_is_replaced_by_a_fixed_code() -> None:
    built = tally()
    built.record("failed", attempted=True, reason="Engine said: no such thing")

    result = built.result(dry_run=False)

    assert result.reason is not None
    assert result.reason.isidentifier() or "_" in result.reason
    assert "Engine" not in result.reason


def test_extra_detail_is_added_to_the_counts() -> None:
    result = counted("done").result(dry_run=False, extra={"matched": 4, "unhealthy": 1})

    assert detail(result) == {"restarted": 1, "matched": 4, "unhealthy": 1}


def test_a_dry_run_lists_the_planned_names_sorted_and_always_succeeds() -> None:
    built = tally()
    built.record("done", attempted=False, name="web")
    built.record("done", attempted=False, name="api")
    built.record("exhausted", attempted=False, reason="restart_budget_exhausted")
    built.record("failed", attempted=False, reason="permission_denied")

    result = built.result(dry_run=True)

    assert result.outcome == "success"
    assert detail(result) == {
        "would_restart_count": 2,
        "would_restart": "api, web",
        "failed": 1,
        "exhausted": 1,
    }
    assert result.attempted == 0


def test_a_long_plan_is_cut_to_the_detail_limit_with_a_count_of_the_rest() -> None:
    built = tally()
    for number in range(100):
        built.record("done", attempted=False, name=f"container-{number:03d}")

    result = built.result(dry_run=True)

    text = detail(result)["would_restart"]
    assert len(text) <= DETAIL_TEXT_LIMIT
    assert text.startswith("container-000, container-001")
    assert text.endswith(" more")
    assert detail(result)["would_restart_count"] == 100


class Work:
    """The work act_on runs: records an event under the run, then does what the test says."""

    def __init__(self, ctx: Any, *, record: bool = True, raises: BaseException | None = None):
        self.context = ctx
        self.calls: list[AutomationAuthority] = []
        self._record = record
        self._raises = raises

    async def __call__(self, authority: AutomationAuthority) -> None:
        self.calls.append(authority)
        if self._record:
            await self.context.activity.record_event(
                authority.subsystem_actor,
                action="container_restarted",
                target_type="container",
                target_id=KEY.object_id,
                environment_id=ENV_A,
                result="success",
            )
        if self._raises is not None:
            raise self._raises


async def acted(
    context: Any,
    work: Work,
    *,
    explicit: bool = True,
    single: bool = False,
    built: ObjectTally | None = None,
    **options: Any,
) -> ObjectStatus:
    return await act_on(
        context, KEY, explicit=explicit, work=work, tally=built, single=single, **options
    )


async def test_authorised_work_is_run_with_the_runs_authority_and_counted_as_attempted(
    rig: Rig,
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        work = Work(context)
        built = tally()

        status = await acted(context, work, built=built, name="web")

    assert status == "done"
    assert [call.actor for call in work.calls] == [context.actor]
    assert built.result(dry_run=False).attempted == 1
    assert detail(built.result(dry_run=False)) == {"restarted": 1}


async def test_work_that_wrote_no_event_is_not_counted_as_attempted(rig: Rig) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        await acted(context, Work(context, record=False), built=built)

    assert built.result(dry_run=False).attempted == 0


@pytest.mark.parametrize(
    ("explicit", "status", "recorded", "reason"),
    [
        (True, "failed", {"failed": 1}, PERMISSION_DENIED),
        (False, "not_permitted", {"not_permitted": 1}, None),
    ],
)
async def test_an_object_the_owner_may_not_change_is_never_acted_on(
    rig: Rig, explicit: bool, status: str, recorded: dict[str, int], reason: str | None
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=Gate(allow=lambda _key: False))
        work = Work(context)
        built = tally()

        outcome = await acted(context, work, explicit=explicit, built=built)

    assert outcome == status
    assert work.calls == []
    assert detail(built.result(dry_run=False)) == {"restarted": 0, **recorded}
    assert built.result(dry_run=False).reason == reason


async def test_work_refused_for_dockmins_own_container_is_a_skip_not_a_failure(rig: Rig) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        status = await acted(
            context, Work(context, record=False, raises=SelfProtected.container()), built=built
        )

    result = built.result(dry_run=False)
    assert status == "skipped"
    assert (result.outcome, result.attempted) == ("success", 0)
    assert detail(result)["self_protected"] == 1


async def test_the_owner_is_asked_before_the_work_runs(rig: Rig) -> None:
    gate = Gate()
    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=gate)

        await acted(context, Work(context))

    assert gate.checked == [KEY]


async def test_an_unreachable_host_during_the_owner_check_fails_the_object(rig: Rig) -> None:
    class Down(Gate):
        async def authorise(self, key: ObjectKey) -> None:
            raise EnvironmentUnavailable("unreachable")

    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=Down())
        built = tally()

        status = await acted(context, Work(context), built=built)

    assert status == "failed"
    assert built.result(dry_run=False).reason == "environment_unavailable"


async def test_an_unreachable_host_during_the_owner_check_propagates_without_a_tally(
    rig: Rig,
) -> None:
    class Down(Gate):
        async def authorise(self, key: ObjectKey) -> None:
            raise EnvironmentUnavailable("unreachable")

    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=Down())

        with pytest.raises(EnvironmentUnavailable):
            await check_authorised(context, KEY, explicit=True)


async def test_a_recent_user_action_suppresses_the_object_in_a_multi_object_run(rig: Rig) -> None:
    async with rig.database.session() as db:
        suppressions = SuppressionRegistry(rig.clock)
        suppressions.note(KEY)
        context = await run_context(rig, db, suppressions=suppressions)
        work = Work(context)
        built = tally()

        status = await acted(context, work, built=built)

    assert status == "suppressed"
    assert work.calls == []
    assert detail(built.result(dry_run=False)) == {"restarted": 0, "suppressed": 1}


async def test_a_recent_user_action_skips_a_single_object_run_with_the_time_of_the_action(
    rig: Rig,
) -> None:
    async with rig.database.session() as db:
        suppressions = SuppressionRegistry(rig.clock)
        suppressions.note(KEY)
        context = await run_context(rig, db, suppressions=suppressions)

        with pytest.raises(RunSkipped) as skipped:
            await acted(context, Work(context), single=True)

    assert skipped.value.reason == "suppressed"
    assert "acted_at" in skipped.value.detail


async def test_a_user_action_older_than_the_window_does_not_suppress(rig: Rig) -> None:
    async with rig.database.session() as db:
        suppressions = SuppressionRegistry(rig.clock)
        suppressions.note(KEY)
        rig.clock.jump(timedelta(minutes=6).total_seconds())
        context = await run_context(rig, db, suppressions=suppressions)

        status = await acted(context, Work(context))

    assert status == "done"


@pytest.mark.parametrize(
    ("raised", "status"),
    [
        (ObjectBusy(LockHolder(UserActor.of(ADMIN), "admin-user", "stop")), "busy"),
        (RunSkipped("suppressed", "A user acted on the object recently."), "suppressed"),
    ],
)
async def test_contention_met_inside_the_work_is_a_status_in_a_multi_object_run(
    rig: Rig, raised: BaseException, status: str
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        outcome = await acted(context, Work(context, record=False, raises=raised), built=built)

    assert outcome == status
    assert detail(built.result(dry_run=False)) == {"restarted": 0, status: 1}


@pytest.mark.parametrize(
    "raised",
    [
        ObjectBusy(LockHolder(UserActor.of(ADMIN), "admin-user", "stop")),
        RunSkipped("suppressed", "A user acted on the object recently."),
    ],
)
async def test_contention_met_inside_the_work_escapes_a_single_object_run(
    rig: Rig, raised: BaseException
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)

        with pytest.raises(type(raised)):
            await acted(context, Work(context, record=False, raises=raised), single=True)


@pytest.mark.parametrize(
    "raised",
    [
        RunSkipped("object_busy", "Busy."),
        RunFailed("compose_unavailable", "Compose is not available."),
    ],
)
async def test_a_skip_for_another_reason_or_a_run_failure_ends_the_run(
    rig: Rig, raised: BaseException
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)

        with pytest.raises(type(raised)):
            await acted(context, Work(context, record=False, raises=raised))


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (PermissionDenied("insufficient access"), "permission_denied"),
        (ResourceNotFound("not found"), "resource_not_found"),
        (RuntimeError("Engine said something private"), "runtime_error"),
    ],
)
async def test_an_error_from_the_work_fails_the_object_under_a_reason_from_its_class(
    rig: Rig, raised: BaseException, reason: str
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        status = await acted(context, Work(context, record=False, raises=raised), built=built)

    assert status == "failed"
    assert built.result(dry_run=False).reason == reason
    assert "private" not in repr(built.result(dry_run=False))


async def test_an_unexpected_error_is_logged_by_class_name_only(
    rig: Rig, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)

        await acted(
            context, Work(context, record=False, raises=RuntimeError("Engine said something"))
        )

    [record] = logged("subsystem_object_failed")
    assert record["error"] == "RuntimeError"
    assert "Engine said something" not in str(record)


async def test_work_that_failed_after_writing_its_failure_row_is_counted_as_attempted(
    rig: Rig,
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        await acted(
            context, Work(context, raises=PermissionDenied("insufficient access")), built=built
        )

    assert built.result(dry_run=False).attempted == 1


async def test_success_can_be_reported_under_another_status_and_reason(rig: Rig) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        built = tally()

        status = await acted(
            context,
            Work(context),
            built=built,
            on_success="exhausted",
            success_reason="restart_budget_exhausted",
        )

    assert status == "exhausted"
    assert built.result(dry_run=False).reason == "restart_budget_exhausted"


async def test_a_dry_run_forecasts_a_busy_object_without_taking_its_lock(rig: Rig) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db)
        holder = LockHolder(UserActor.of(ADMIN), "admin-user", "stop")
        keys = [KEY, OTHER_KEY]

        async with context.locks.hold(OTHER_KEY, holder):
            many = forecast_busy(context.locks, keys, single=False)
            with pytest.raises(ObjectBusy) as busy:
                forecast_busy(context.locks, keys, single=True)
        free = forecast_busy(context.locks, keys, single=False)

    assert many is True
    assert (busy.value.holder.label, busy.value.holder.action) == ("admin-user", "stop")
    assert free is False


class Stated(ResourceNotFound):
    def __init__(self, reason: object) -> None:
        super().__init__("x")
        self.reason = reason


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ServiceUpdateFailed("update_paused", "paused", 1), "update_paused"),
        (StackRejected({"stack": "x"}, "would_remove"), "would_remove"),
        (Stated("Not A Reason"), "stated"),
        (Stated(7), "stated"),
        (Stated(""), "stated"),
        (PermissionDenied("x"), "permission_denied"),
        (ResourceNotFound("x"), "resource_not_found"),
    ],
    ids=[
        "service-reason",
        "stack-reason",
        "invalid-text",
        "not-text",
        "empty",
        "no-reason-class",
        "no-reason-class-2",
    ],
)
def test_a_failed_objects_reason_is_the_one_the_error_states_else_one_from_its_class(
    error: ProductError, reason: str
) -> None:
    assert failure_reason(error) == reason


class TooLarge(Gate):
    async def authorise(self, key: ObjectKey) -> None:
        raise ContainerListTooLarge()


async def test_a_host_too_large_to_read_during_the_owner_check_fails_that_object_alone(
    rig: Rig,
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=TooLarge())
        built = tally()

        status = await acted(context, Work(context), built=built)

    result = built.result(dry_run=False)
    assert status == "failed"
    assert (result.outcome, result.reason) == ("failure", "container_list_too_large")


async def test_a_host_too_large_to_read_propagates_when_there_is_no_tally_to_settle_it_in(
    rig: Rig,
) -> None:
    async with rig.database.session() as db:
        context = await run_context(rig, db, gate=TooLarge())

        with pytest.raises(ObjectTooLarge):
            await check_authorised(context, KEY, explicit=True)


class RefusingAuthoriser:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def forget_decisions(self) -> None:
        return None

    async def require_access(self, *arguments: Any) -> None:
        raise self.error


async def current_owner() -> bool:
    return True


@pytest.mark.parametrize(
    ("error", "standing"),
    [
        pytest.param(PermissionDenied("x"), "readable", id="read-only"),
        pytest.param(ContainerListTooLarge(), "readable", id="too-large-to-read"),
        pytest.param(ResourceNotFound("x"), "hidden", id="hidden"),
    ],
)
async def test_an_owners_standing_over_an_object_follows_what_the_check_raises(
    error: Exception, standing: str
) -> None:
    gate = OwnerGate(
        STANDARD,
        RefusingAuthoriser(error),  # type: ignore[arg-type]
        current_owner,
    )

    assert await gate.standing(KEY) == standing
