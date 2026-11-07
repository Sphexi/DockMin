"""The verification stage of an automated recreate, over captured samples and a fake daemon."""

import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest

from app.auth.errors import PermissionDenied
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerInspect
from app.services import container_recreate
from app.services.acting import AutomationAuthority
from app.services.activity import ActivityRecorder, UserActor
from app.services.container_provisioning import (
    MAX_VERIFY_SECONDS,
    NotRunning,
    RecreateFailed,
    RecreateRun,
    RestrictedOptions,
    VerifyPolicy,
    settle_seconds,
)
from app.services.container_recreate import (
    ContainerRecreateService,
    _crash_verdict,
    _health_bound,
    _healthcheck_of,
)
from app.services.image_jobs import JobProgress
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import CredentialChoice
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import Gate, authority_context, event_rows
from tests.docker_engine.container_engine import Box, ContainerEngine, created_id
from tests.provisioning_support import OLD, ResolverStub, seed_recreatable
from tests.services.conftest import ContainerRig
from tests.updater_support import HEALTH_NS, Replay

UPDATER = "container-updater"
NEW = created_id(1)
CHECK_INTERVAL = 5 * HEALTH_NS
HEALTH = {
    "Test": ["CMD", "true"],
    "Interval": CHECK_INTERVAL,
    "Timeout": HEALTH_NS,
    "StartPeriod": 0,
}
UNSET_HEALTH = {"Test": ["CMD", "true"]}
NOTE = {"trigger": "update", "update_reason": "update_available"}
POLL = 2.0
SHORT_WAIT = 0.05
BASELINE_RESTARTS = 4


def bare_service(sleep: Callable[[float], Any]) -> ContainerRecreateService:
    nothing = cast(Any, None)
    return ContainerRecreateService(
        db=nothing,
        database=nothing,
        authoriser=nothing,
        inspector=cast(Any, type("Inspector", (), {"hosts": None})()),
        locks=nothing,
        controls=nothing,
        activity=nothing,
        clock=nothing,
        credentials=nothing,
        sleep=sleep,
    )


@pytest.mark.parametrize(
    ("shape", "policy", "failure", "exit_code", "healthcheck", "polls"),
    [
        ("exit3", VerifyPolicy(30, False), "exited", 1, False, 2),
        ("restartloop", VerifyPolicy(30, False), "restarting", None, False, 0),
        ("unhealthy", VerifyPolicy(4, True), "unhealthy", None, True, 2),
        ("healthy", VerifyPolicy(4, True), None, None, True, 2),
        ("nohealth", VerifyPolicy(4, True), None, None, False, 2),
        ("nonehealth", VerifyPolicy(4, True), None, None, False, 2),
        ("defaultinterval", VerifyPolicy(30, False), None, None, True, 15),
        ("defaultinterval", VerifyPolicy(30, True), None, None, True, 16),
    ],
)
async def test_the_watch_judges_each_captured_shape(
    shape: str,
    policy: VerifyPolicy,
    failure: str | None,
    exit_code: int | None,
    healthcheck: bool,
    polls: int,
) -> None:
    replay = Replay(shape)
    service = bare_service(replay.sleep)

    verdict = await service._watch(cast(Any, replay), "container", policy, RecreateRun())

    assert (verdict.failure, verdict.exit_code, verdict.healthcheck) == (
        failure,
        exit_code,
        healthcheck,
    )
    assert replay.sleeps == [POLL] * polls


@pytest.mark.parametrize(
    ("shape", "index", "baseline", "failure"),
    [
        ("exit3", 0, 0, None),
        ("exit3", 3, 0, "exited"),
        ("restartloop", 0, 0, "restarting"),
        ("restartloop", 4, BASELINE_RESTARTS, "restarted"),
        ("restartloop", 4, 5, None),
        ("startfail", 0, 0, "not_running"),
    ],
)
def test_a_crash_is_judged_from_state_and_restart_count(
    shape: str, index: int, baseline: int, failure: str | None
) -> None:
    sample = Replay(shape).sample(index)

    verdict = _crash_verdict(sample, baseline)

    assert (None if verdict is None else verdict.failure) == failure


@pytest.mark.parametrize(
    ("shape", "index", "present", "bound"),
    [
        ("defaultinterval", 0, True, 65.0),
        ("starting", 0, True, 67.0),
        ("healthy", 0, True, 7.0),
        ("nohealth", 0, False, None),
        ("nonehealth", 0, False, None),
    ],
)
def test_the_health_window_comes_from_the_containers_own_healthcheck(
    shape: str, index: int, present: bool, bound: float | None
) -> None:
    check = _healthcheck_of(Replay(shape).sample(index).config_document)

    assert (check is not None) is present
    if check is not None:
        assert _health_bound(check) == bound


class _Failing:
    def __init__(self, error: EngineError) -> None:
        self.containers = self
        self._error = error

    async def inspect(self, container_id: str) -> ContainerInspect:
        raise self._error


@pytest.mark.parametrize(
    ("error", "failure"),
    [(NotFound(ENV_A, "gone"), "gone"), (EngineError(ENV_A, "down"), "unverifiable")],
)
async def test_a_container_that_cannot_be_inspected_fails_verification(
    error: EngineError, failure: str
) -> None:
    async def never(seconds: float) -> None:
        raise AssertionError("no poll follows a failed baseline")

    service = bare_service(never)

    verdict = await service._watch(
        cast(Any, _Failing(error)), "container", VerifyPolicy(30, True), RecreateRun()
    )

    assert verdict.failure == failure


@pytest.mark.parametrize(
    ("seconds", "require_healthy", "longest"),
    [(30, False, 30.0), (30, True, 300.0), (0, False, 0.0), (MAX_VERIFY_SECONDS, True, 300.0)],
)
def test_the_longest_watch_and_the_settle_wait_follow_the_policy(
    seconds: int, require_healthy: bool, longest: float
) -> None:
    policy = VerifyPolicy(seconds, require_healthy)

    assert policy.longest_window == longest
    assert settle_seconds(None, policy) == settle_seconds() + longest
    assert settle_seconds(7, policy) == settle_seconds(7) + longest


@pytest.mark.parametrize(
    "arguments",
    [{"seconds": -1}, {"seconds": MAX_VERIFY_SECONDS + 1}, {"seconds": 1, "interval": 0}],
)
def test_a_policy_outside_what_a_run_can_wait_out_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        VerifyPolicy(require_healthy=False, **arguments)


class Sleeper:
    """The service's sleep: records the wait and lets a test change the new container each time."""

    def __init__(
        self, host: ContainerEngine, script: Callable[[int, Box], None] | None = None
    ) -> None:
        self.host = host
        self.script = script
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.script is not None:
            self.script(len(self.calls), self.host.boxes[NEW])


def service_with(rig: ContainerRig, sleeper: Sleeper) -> ContainerRecreateService:
    return ContainerRecreateService(
        db=rig.db,
        database=rig.database,
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.engine.locks,
        controls=rig.access.controls,
        activity=rig.recorder,
        clock=rig.runtime.clock,
        credentials=ResolverStub(),
        sleep=sleeper,
    )


async def authority(rig: ContainerRig, **options: Any) -> AutomationAuthority:
    context = await authority_context(
        rig.db, rig.runtime.clock, rig.locks, environment_id=ENV_A, subsystem=UPDATER, **options
    )
    return context.authority()


async def recreate(
    rig: ContainerRig,
    sleeper: Sleeper,
    policy: VerifyPolicy | None,
    acting: AutomationAuthority | None = None,
    **options: Any,
) -> Any:
    return await service_with(rig, sleeper).recreate(
        acting or await authority(rig),
        ENV_A,
        OLD,
        expected_name="web",
        verify=policy,
        note=NOTE,
        **options,
    )


def original_is_back(host: ContainerEngine) -> None:
    assert NEW not in host.boxes
    assert (host.boxes[OLD].name, host.boxes[OLD].state) == ("web", "running")


def crashes_on_first_poll(number: int, box: Box) -> None:
    if number == 1:
        box.state = "exited"
        box.exit_code = 1


async def test_a_failed_verification_restores_the_original_and_records_why(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    sleeper = Sleeper(host, crashes_on_first_poll)

    with pytest.raises(RecreateFailed) as failed:
        await recreate(rig, sleeper, VerifyPolicy(30, True))

    error = failed.value
    assert (error.rolled_back, error.stage, error.verify_failure) == (True, "verify", "exited")
    assert error.failed_container_id == NEW
    original_is_back(host)
    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["actor_subsystem"], row["actor_user_id"]) == (
        "failure",
        UPDATER,
        None,
    )
    detail = row["detail"]
    assert detail["reason"] == "verification_failed"
    assert (detail["verified"], detail["verify_failure"], detail["exit_code"]) == (
        False,
        "exited",
        1,
    )
    assert detail["rolled_back"] is True
    assert (detail["trigger"], detail["update_reason"]) == ("update", "update_available")
    assert detail["rule_name"]
    assert "Error" not in str(detail)


async def test_every_step_of_a_failed_verification_is_reported_to_a_feed(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    feed = JobProgress(pull_only=True)

    with pytest.raises(RecreateFailed):
        await recreate(
            rig, Sleeper(host, crashes_on_first_poll), VerifyPolicy(30, True), progress=feed
        )

    assert feed.snapshot().stages == ("stop", "rename", "create", "start", "verify", "rollback")
    original_is_back(host)


async def test_a_start_failure_rolls_back_without_reaching_verification(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    host.fail("start", 400, target=NEW)
    sleeper = Sleeper(host)

    with pytest.raises(RecreateFailed) as failed:
        await recreate(rig, sleeper, VerifyPolicy(30, True))

    error = failed.value
    assert (error.rolled_back, error.stage, error.failed_container_id) == (True, "start", NEW)
    assert sleeper.calls == []
    original_is_back(host)
    [row] = await event_rows(rig.db, "container_recreated")
    assert row["detail"]["rolled_back"] is True
    assert "verified" not in row["detail"]


async def test_a_healthy_container_replaces_the_original_and_the_row_says_it_was_watched(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, config={"Healthcheck": UNSET_HEALTH})
    healthy_at_poll = 16
    sleeper = Sleeper(
        host,
        lambda number, box: setattr(
            box, "health", "healthy" if number >= healthy_at_poll else "starting"
        ),
    )

    outcome = await recreate(rig, sleeper, VerifyPolicy(30, True))

    assert outcome.verified is True
    assert len(sleeper.calls) == healthy_at_poll
    assert OLD not in host.boxes and outcome.new_id in host.boxes
    [row] = await event_rows(rig.db, "container_recreated")
    detail = row["detail"]
    assert (row["result"], detail["verified"], detail["healthcheck"]) == ("success", True, True)
    assert detail["verify_seconds"] == 30


async def test_an_unhealthy_container_fails_at_the_end_of_the_base_window(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, config={"Healthcheck": HEALTH})
    sleeper = Sleeper(
        host, lambda number, box: setattr(box, "health", "starting" if number == 1 else "unhealthy")
    )

    with pytest.raises(RecreateFailed) as failed:
        await recreate(rig, sleeper, VerifyPolicy(4, True))

    assert failed.value.verify_failure == "unhealthy"
    assert sleeper.calls == [POLL, POLL]
    original_is_back(host)
    [row] = await event_rows(rig.db, "container_recreated")
    assert row["detail"]["healthcheck"] is True


async def test_a_healthcheck_still_starting_fails_when_its_own_bound_passes(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, config={"Healthcheck": HEALTH})
    sleeper = Sleeper(host, lambda number, box: setattr(box, "health", "starting"))
    bound_polls = 6

    with pytest.raises(RecreateFailed) as failed:
        await recreate(rig, sleeper, VerifyPolicy(4, True))

    assert failed.value.verify_failure == "not_healthy"
    assert len(sleeper.calls) == bound_polls
    original_is_back(host)


async def test_a_starting_healthcheck_is_accepted_when_health_is_not_required(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, config={"Healthcheck": HEALTH})
    sleeper = Sleeper(host, lambda number, box: setattr(box, "health", "starting"))

    outcome = await recreate(rig, sleeper, VerifyPolicy(4, False))

    assert outcome.verified is True
    assert len(sleeper.calls) == 2
    assert OLD not in host.boxes


async def test_a_run_may_not_pass_what_only_the_other_path_may(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    acting = await authority(rig)
    sleeper = Sleeper(host)

    with pytest.raises(ValueError):
        await service_with(rig, sleeper).recreate(
            ADMIN, ENV_A, OLD, expected_name="web", verify=VerifyPolicy(1, False)
        )
    with pytest.raises(ValueError):
        await recreate(rig, sleeper, None, acting, pull=True)
    with pytest.raises(ValueError):
        await recreate(rig, sleeper, None, acting, credential_choice=CredentialChoice(None))

    assert host.mutations() == []


@pytest.mark.parametrize("state", ["paused", "exited"])
async def test_a_container_that_is_not_running_is_refused_before_anything_is_stopped(
    rig: ContainerRig, host: ContainerEngine, state: str
) -> None:
    seed_recreatable(host, state=state)

    with pytest.raises(NotRunning):
        await recreate(rig, Sleeper(host), VerifyPolicy(30, True))

    assert host.mutations() == []
    assert await event_rows(rig.db, "container_recreated") == []


async def test_a_busy_container_or_stack_is_refused_with_no_row(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, config={"Labels": {"com.docker.compose.project": "shop"}})
    acting = await authority(rig, lock_wait=0.05)
    holder = LockHolder(UserActor.of(ADMIN), "admin", "stop")

    async with rig.locks.hold(ObjectKey(ENV_A, "container", OLD), holder):
        with pytest.raises(ObjectBusy):
            await recreate(rig, Sleeper(host), None, acting)
    async with rig.locks.hold(ObjectKey(ENV_A, "stack", "shop"), holder):
        with pytest.raises(ObjectBusy):
            await recreate(rig, Sleeper(host), None, acting)

    assert host.mutations() == []
    assert await event_rows(rig.db, "container_recreated") == []


async def test_a_cancelled_recreate_is_recorded_under_the_rule_and_frees_its_locks(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    stop = host.hold("stop")
    acting = await authority(rig)
    task = asyncio.ensure_future(recreate(rig, Sleeper(host), VerifyPolicy(4, False), acting))
    await stop.entered.wait()

    task.cancel()
    stop.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["actor_subsystem"], row["rule_id"]) == (
        "success",
        UPDATER,
        acting.actor.rule_id,
    )
    assert row["actor_user_id"] is None
    assert (row["detail"]["interrupted"], row["detail"]["verified"]) == (True, True)
    assert len(rig.locks) == 0


class Called:
    def __init__(self, rig: ContainerRig) -> None:
        self.rig = rig
        self.failures: list[RecreateFailed] = []
        self.rows_seen: list[int] = []

    async def __call__(self, failure: RecreateFailed, activity: ActivityRecorder) -> None:
        self.failures.append(failure)
        self.rows_seen.append(len(await event_rows(self.rig.db, "container_recreated")))


class CancelsOnFirstPoll(Sleeper):
    def __init__(self, host: ContainerEngine) -> None:
        super().__init__(host, crashes_on_first_poll)
        self.task: asyncio.Future[Any] | None = None

    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        if self.task is not None:
            self.task.cancel()


class HoldsOriginalRestart:
    """Holds the start request that restores the original, the second the daemon receives."""

    def __init__(self, host: ContainerEngine) -> None:
        self.starts = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        host.on("start", self)

    async def __call__(self) -> None:
        self.starts += 1
        if self.starts == 2:
            self.entered.set()
            await self.release.wait()


class StallsOnPoll(Sleeper):
    """Runs the script on each poll before `stall_at` and never returns from that poll."""

    def __init__(
        self,
        host: ContainerEngine,
        script: Callable[[int, Box], None] | None = None,
        *,
        stall_at: int = 2,
    ) -> None:
        super().__init__(host, script)
        self.stall_at = stall_at
        self.entered = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        if len(self.calls) + 1 >= self.stall_at:
            self.entered.set()
            await asyncio.Event().wait()
        await super().__call__(seconds)


async def test_a_cancel_during_a_failing_verification_calls_back_after_the_interrupted_row(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    called = Called(rig)
    sleeper = CancelsOnFirstPoll(host)
    sleeper.task = asyncio.ensure_future(
        recreate(rig, sleeper, VerifyPolicy(30, True), on_rolled_back=called)
    )

    with pytest.raises(asyncio.CancelledError):
        await sleeper.task

    original_is_back(host)
    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["detail"]["interrupted"]) == ("failure", True)
    assert [(f.rolled_back, f.settled_late) for f in called.failures] == [(True, False)]
    assert called.rows_seen == [1]


async def test_a_failure_that_settles_after_the_wait_writes_a_late_row_and_calls_back_once(
    rig: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_recreate, "settle_seconds", lambda *_: SHORT_WAIT)
    seed_recreatable(host)
    called = Called(rig)
    restart = HoldsOriginalRestart(host)
    task = asyncio.ensure_future(
        recreate(
            rig,
            Sleeper(host, crashes_on_first_poll),
            VerifyPolicy(30, True),
            on_rolled_back=called,
        )
    )
    await restart.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert called.failures == []

    restart.release.set()
    for _ in range(2):
        await asyncio.gather(
            *(asyncio.all_tasks() - {asyncio.current_task()}), return_exceptions=True
        )

    first, late = await event_rows(rig.db, "container_recreated")
    assert (first["detail"]["reason"], late["detail"]["reason"]) == (
        "outcome_unknown",
        "verification_failed",
    )
    assert (late["result"], late["detail"]["settled_late"]) == ("failure", True)
    assert [(f.rolled_back, f.settled_late) for f in called.failures] == [(True, True)]


@pytest.mark.parametrize(
    ("script", "verify_failure"),
    [
        pytest.param(
            lambda number, box: setattr(box, "health", "unhealthy"), "unhealthy", id="unhealthy"
        ),
        pytest.param(
            lambda number, box: setattr(box, "health", "starting"), "interrupted", id="undecided"
        ),
    ],
)
async def test_a_cancel_during_the_watch_rolls_back_at_once_and_records_what_was_seen(
    rig: ContainerRig,
    host: ContainerEngine,
    script: Callable[[int, Box], None],
    verify_failure: str,
) -> None:
    seed_recreatable(host, config={"Healthcheck": HEALTH})
    called = Called(rig)
    sleeper = StallsOnPoll(host, script)
    task = asyncio.ensure_future(
        recreate(rig, sleeper, VerifyPolicy(30, True), on_rolled_back=called)
    )
    await sleeper.entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    original_is_back(host)
    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["detail"]["interrupted"], row["detail"]["rolled_back"]) == (
        "failure",
        True,
        True,
    )
    assert row["detail"]["verify_failure"] == verify_failure
    assert [(f.rolled_back, f.verify_failure) for f in called.failures] == [(True, verify_failure)]
    assert called.rows_seen == [1]


async def test_a_cancel_outside_verification_does_not_call_back(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    called = Called(rig)
    stop = host.hold("stop")
    task = asyncio.ensure_future(
        recreate(rig, Sleeper(host), VerifyPolicy(4, False), on_rolled_back=called)
    )
    await stop.entered.wait()

    task.cancel()
    stop.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)
    assert called.failures == []


async def test_a_cancelled_recreate_whose_rollback_fails_does_not_call_back(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    host.fail("start", 500, target=NEW)
    host.fail("rename", 500, skip=1)
    called = Called(rig)
    stop = host.hold("stop")
    task = asyncio.ensure_future(
        recreate(rig, Sleeper(host), VerifyPolicy(4, False), on_rolled_back=called)
    )
    await stop.entered.wait()

    task.cancel()
    stop.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await event_rows(rig.db, "container_recreated")
    assert (row["result"], row["detail"]["interrupted"]) == ("failure", True)
    assert row["detail"]["rollback_failed"] is True
    assert called.failures == []


async def test_a_rollback_callback_is_refused_without_a_verifying_automated_recreate(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    called = Called(rig)

    with pytest.raises(ValueError):
        await recreate(rig, Sleeper(host), None, on_rolled_back=called)
    with pytest.raises(ValueError):
        await service_with(rig, Sleeper(host)).recreate(
            ADMIN,
            ENV_A,
            OLD,
            expected_name="web",
            on_rolled_back=called,
        )

    assert host.mutations() == []


class _StandardOwner(Gate):
    def __init__(self) -> None:
        super().__init__()
        self._principal = STANDARD

    @property
    def is_administrator(self) -> bool:
        return False

    async def authorise_read(self, key: ObjectKey) -> None:
        return None


async def test_a_standard_owner_is_held_to_the_restriction_set(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, host_config={"Privileged": True})
    context = await authority_context(
        rig.db, rig.runtime.clock, rig.locks, environment_id=ENV_A, subsystem=UPDATER
    )
    context.owner_gate = _StandardOwner()
    standard = context.authority()

    with pytest.raises(RestrictedOptions):
        await recreate(rig, Sleeper(host), None, standard)

    assert standard.owner_is_administrator is False
    assert host.mutations() == []


async def test_an_owner_refused_on_the_container_changes_nothing(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    context = await authority_context(
        rig.db,
        rig.runtime.clock,
        rig.locks,
        environment_id=ENV_A,
        subsystem=UPDATER,
        allow=lambda key: False,
    )

    with pytest.raises(PermissionDenied):
        await recreate(rig, Sleeper(host), None, context.authority())

    assert host.mutations() == []
