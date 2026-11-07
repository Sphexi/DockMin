"""Stack jobs: the registry's caps and visibility, progress, outcomes, and the job entry point."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected
from app.auth.principal import Principal
from app.compose_cli.parsers import ProgressEvent
from app.services.assembly import start_stack_job
from app.services.stack_deploy import (
    CONFIG_SLOT_WAIT_SECONDS,
    StackBusy,
    StackOperationFailed,
    StackRejected,
)
from app.services.stack_jobs import (
    MAX_JOBS_PER_PROCESS,
    MAX_JOBS_PER_USER,
    StackJobRegistry,
    StackProgress,
    TooManyStackJobs,
)
from app.services.stack_views import StackOutcome
from tests.access_support import ADMIN, ENV_A, ENV_B, MEMBER, PEER, STANDARD
from tests.auth_support import FakeClock
from tests.image_support import REFERENCE
from tests.stack_support import (
    Rig,
    create_then_deploy,
    poll_until,
    read_records,
    stored_file_digest,
    use_compose_limits,
    wait_job,
)
from tests.support import present

STACK = "shop"
LAYER = "aaaaaaaaaaaa"
SLOT_WAIT = 0.05
SHORT_TIMEOUT = 1.0
PLAIN_STACK = """
services:
  web:
    image: nginx:1
"""
PULLING_STACK = f"""
services:
  web:
    image: nginx:1
  app:
    image: {REFERENCE}
"""

type Run = Callable[[StackProgress], Awaitable[StackOutcome]]


def finished() -> StackOutcome:
    return StackOutcome(
        operation="deploy",
        environment_id=ENV_A,
        name=STACK,
        message="Deployed shop.",
        status="active",
        events=2,
        exit_code=0,
        pulled=(),
        services=(),
        services_running=1,
    )


def opened_gate() -> asyncio.Event:
    gate = asyncio.Event()
    gate.set()
    return gate


def blocked_by(gate: asyncio.Event) -> Run:
    async def run(progress: StackProgress) -> StackOutcome:
        await gate.wait()
        return finished()

    return run


def failing_with(error: Exception) -> Run:
    async def run(progress: StackProgress) -> StackOutcome:
        raise error

    return run


def event(
    resource: str | None, status: str, text: str = "", *, error: bool = False
) -> ProgressEvent:
    return ProgressEvent(resource, None, status, text, error, None, None)


def begin(registry: StackJobRegistry, owner: Principal, run: Run) -> str:
    return registry.start("redeploy", owner, ENV_A, STACK, run)


@pytest.fixture
async def registry() -> AsyncIterator[StackJobRegistry]:
    jobs = StackJobRegistry()
    yield jobs
    await jobs.aclose()


async def ended(registry: StackJobRegistry, job_id: str) -> None:
    await poll_until(lambda: not registry.view(job_id, ADMIN).running)


async def test_the_registry_caps_running_jobs_per_user_and_per_process(
    registry: StackJobRegistry,
) -> None:
    gate = asyncio.Event()
    first = begin(registry, ADMIN, blocked_by(gate))
    second = begin(registry, ADMIN, blocked_by(gate))

    with pytest.raises(TooManyStackJobs) as per_user:
        begin(registry, ADMIN, blocked_by(gate))

    begin(registry, STANDARD, blocked_by(gate))
    begin(registry, STANDARD, blocked_by(gate))
    with pytest.raises(TooManyStackJobs) as per_process:
        begin(registry, MEMBER, blocked_by(gate))

    assert (MAX_JOBS_PER_USER, MAX_JOBS_PER_PROCESS) == (2, 4)
    assert per_user.value.scope == "user"
    assert per_process.value.scope == "process"
    gate.set()
    await ended(registry, first)
    await ended(registry, second)
    begin(registry, ADMIN, blocked_by(gate))


async def test_operations_run_in_a_request_reserve_slots_under_the_same_caps(
    registry: StackJobRegistry,
) -> None:
    gate = asyncio.Event()

    with registry.reserve(ADMIN):
        begin(registry, ADMIN, blocked_by(gate))
        with pytest.raises(TooManyStackJobs) as per_user, registry.reserve(ADMIN):
            pass
        with (
            registry.reserve(STANDARD),
            registry.reserve(MEMBER),
            pytest.raises(TooManyStackJobs) as per_process,
        ):
            begin(registry, PEER, blocked_by(gate))

    assert per_user.value.scope == "user"
    assert per_process.value.scope == "process"
    begin(registry, PEER, blocked_by(gate))
    with registry.reserve(ADMIN), registry.reserve(STANDARD):
        pass
    gate.set()


async def test_a_reservation_is_released_when_the_operation_inside_it_fails(
    registry: StackJobRegistry,
) -> None:
    with pytest.raises(ValueError), registry.reserve(ADMIN):
        raise ValueError

    with registry.reserve(ADMIN), registry.reserve(ADMIN):
        pass


async def test_a_job_is_visible_to_its_owner_and_to_administrators_only(
    registry: StackJobRegistry,
) -> None:
    gate = asyncio.Event()
    job_id = begin(registry, STANDARD, blocked_by(gate))

    owner = registry.view(job_id, STANDARD)
    administrator = registry.view(job_id, ADMIN)

    assert (owner.job_id, owner.owner_user_id, owner.stack) == (job_id, STANDARD.user_id, STACK)
    assert administrator.job_id == job_id
    with pytest.raises(ResourceNotFound):
        registry.view(job_id, MEMBER)
    with pytest.raises(ResourceNotFound):
        registry.view("no-such-job", ADMIN)
    gate.set()


async def test_a_job_that_returns_succeeds_with_its_result(registry: StackJobRegistry) -> None:
    job_id = begin(registry, ADMIN, blocked_by(opened_gate()))

    await ended(registry, job_id)

    view = registry.view(job_id, ADMIN)
    assert view.state == "succeeded"
    assert view.finished_at is not None
    assert view.outcome is not None
    assert (view.outcome.ok, view.outcome.message, view.outcome.exit_code) == (
        True,
        "Deployed shop.",
        0,
    )
    assert view.outcome.events == 2
    assert view.outcome.result == finished()


@pytest.mark.parametrize(
    ("error", "reason", "message"),
    [
        pytest.param(
            StackOperationFailed("pull_failed", "Pulling an image failed."),
            "pull_failed",
            "Pulling an image failed.",
            id="operation",
        ),
        pytest.param(
            StackRejected(
                {"compose_file": "Docker Compose rejected this file."}, "invalid_compose"
            ),
            "invalid_compose",
            None,
            id="rejected",
        ),
        pytest.param(PermissionDenied("no"), "forbidden", None, id="forbidden"),
        pytest.param(ResourceNotFound("no"), "not_found", None, id="not-found"),
        pytest.param(SelfProtected.stack("shop"), "self_protected", None, id="self-protected"),
        pytest.param(RuntimeError("secret-detail"), "failed", "The operation failed.", id="crash"),
    ],
)
async def test_a_job_that_raises_fails_with_a_product_written_outcome(
    registry: StackJobRegistry, error: Exception, reason: str, message: str | None
) -> None:
    job_id = begin(registry, ADMIN, failing_with(error))

    await ended(registry, job_id)

    view = registry.view(job_id, ADMIN)
    assert view.state == "failed"
    assert view.outcome is not None
    assert (view.outcome.ok, view.outcome.reason) == (False, reason)
    assert "secret-detail" not in view.outcome.message
    assert message is None or view.outcome.message == message


async def test_a_failed_compose_command_keeps_its_exit_code_stderr_and_progress(
    registry: StackJobRegistry,
) -> None:
    error = StackOperationFailed(
        "compose_failed",
        "Docker Compose exited with status 3.",
        exit_code=3,
        stderr="port is already allocated",
        event_count=2,
        progress="Container shop-web-1 Working",
    )
    job_id = begin(registry, ADMIN, failing_with(error))

    await ended(registry, job_id)

    outcome = registry.view(job_id, ADMIN).outcome
    assert outcome is not None
    assert (outcome.reason, outcome.exit_code, outcome.stderr) == (
        "compose_failed",
        3,
        "port is already allocated",
    )
    assert (outcome.events, outcome.progress) == (2, "Container shop-web-1 Working")


async def test_closing_the_registry_cancels_running_jobs_and_refuses_new_ones() -> None:
    registry = StackJobRegistry()
    job_id = begin(registry, ADMIN, blocked_by(asyncio.Event()))

    await registry.aclose()

    view = registry.view(job_id, ADMIN)
    assert view.state == "cancelled"
    assert view.outcome is not None
    assert (view.outcome.ok, view.outcome.reason) == (False, "outcome_unknown")
    with pytest.raises(RuntimeError):
        begin(registry, ADMIN, blocked_by(asyncio.Event()))


async def test_finished_jobs_are_dropped_after_the_retention_and_beyond_the_keep_limit() -> None:
    clock = FakeClock()
    jobs = StackJobRegistry(clock, keep=2, retention=timedelta(minutes=15))
    oldest = begin(jobs, ADMIN, blocked_by(opened_gate()))
    await ended(jobs, oldest)
    middle = begin(jobs, ADMIN, blocked_by(opened_gate()))
    await ended(jobs, middle)
    newest = begin(jobs, ADMIN, blocked_by(opened_gate()))
    await ended(jobs, newest)

    with pytest.raises(ResourceNotFound):
        jobs.view(oldest, ADMIN)
    assert jobs.view(middle, ADMIN).state == "succeeded"
    assert len(jobs) == 2

    clock.advance(timedelta(minutes=16))
    with pytest.raises(ResourceNotFound):
        jobs.view(newest, ADMIN)
    assert len(jobs) == 0
    await jobs.aclose()


def test_the_latest_event_of_a_resource_replaces_the_last_and_repeats_change_nothing() -> None:
    progress = StackProgress()
    progress.on_event(event("Container shop-web-1", "Working", "Starting"))
    after_first = progress.version

    progress.on_event(event("Container shop-web-1", "Done", "Started"))
    after_second = progress.version
    progress.on_event(event("Container shop-web-1", "Done", "Started"))

    [view] = progress.snapshot().resources
    assert (view.kind, view.name, view.status, view.text) == (
        "Container",
        "shop-web-1",
        "done",
        "Started",
    )
    assert after_second > after_first
    assert progress.version == after_second


def test_events_without_a_resource_are_ignored_and_other_ids_are_shown_as_resources() -> None:
    progress = StackProgress()

    progress.on_event(event(None, "Working"))
    progress.on_event(event("service:web:1", "Error", "no such image", error=True))

    [view] = progress.snapshot().resources
    assert (view.kind, view.name, view.status, view.is_error) == (
        "Resource",
        "service:web:1",
        "error",
        True,
    )
    assert progress.version == 1


def test_resources_beyond_the_tracking_limit_are_counted_not_listed() -> None:
    progress = StackProgress()
    for number in range(500):
        progress.on_event(event(f"Container c{number}", "Done"))

    progress.on_event(event("Container one-too-many", "Done"))

    snapshot = progress.snapshot()
    assert len(snapshot.resources) == 500
    assert snapshot.untracked_resources == 1


def test_a_pull_collector_is_created_once_per_reference_and_listed_in_the_snapshot() -> None:
    progress = StackProgress()

    first = progress.pull(REFERENCE)

    assert progress.pull(REFERENCE) is first
    assert [pull.reference for pull in progress.snapshot().pulls] == [REFERENCE]


async def test_a_create_job_reports_pulls_and_resources_and_ends_with_the_outcome(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    rig.host.pull_lines = [{"status": "Pull complete", "id": LAYER}]

    job_id = start_stack_job(
        rig.app,
        ADMIN,
        ENV_B,
        "create",
        STACK,
        lambda services, progress: create_then_deploy(
            services.actions,
            ADMIN,
            ENV_B,
            name=STACK,
            compose_text=PULLING_STACK,
            progress=progress,
        ),
    )
    view = await wait_job(rig, ADMIN, job_id)

    assert view.state == "succeeded"
    assert view.kind == "create"
    assert view.outcome.result.pulled == (REFERENCE,)
    [pull] = view.progress.pulls
    assert (pull.reference, pull.progress.layers_done) == (REFERENCE, 1)
    resources = {r.resource: r.status for r in view.progress.resources}
    assert resources == {"Container shop-web-1": "done", "Container shop-app-1": "done"}
    assert view.progress.untracked_resources == 0
    with pytest.raises(ResourceNotFound):
        rig.engine.stack_jobs.view(job_id, STANDARD)


async def test_a_job_authorises_again_and_the_stack_lock_decides_between_concurrent_jobs(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await rig.services.actions.create(ADMIN, ENV_B, name=STACK, compose_text=PLAIN_STACK)

    def redeploy(principal: Principal) -> str:
        return start_stack_job(
            rig.app,
            principal,
            ENV_B,
            "redeploy",
            STACK,
            lambda services, progress: services.actions.redeploy(
                principal, ENV_B, STACK, progress=progress
            ),
        )

    outsider = await wait_job(rig, MEMBER, redeploy(MEMBER))
    racing = [redeploy(ADMIN), redeploy(ADMIN)]
    with pytest.raises(TooManyStackJobs):
        redeploy(ADMIN)
    results = [await wait_job(rig, ADMIN, job_id) for job_id in racing]

    assert outsider.state == "failed"
    assert outsider.outcome.reason == "not_found"
    assert sorted((v.state, v.outcome.reason or "") for v in results) == [
        ("failed", "object_busy"),
        ("succeeded", ""),
    ]


async def test_a_create_runs_config_once(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    text = PLAIN_STACK + f"x-fake:\n  record: {record}\n"

    await rig.services.actions.create(ADMIN, ENV_B, name=STACK, compose_text=text)

    assert [call["cmd"] for call in read_records(record)] == ["config"]


async def test_a_mutation_waits_for_a_compose_slot_only_within_its_deadline(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.services.actions, ADMIN, ENV_B, name=STACK, compose_text=PLAIN_STACK
    )
    rig.sim = await use_compose_limits(
        rig.compose, rig.host, operation_timeout=SHORT_TIMEOUT, slots=1
    )

    async with rig.sim.slot(None):
        async with asyncio.timeout(10):
            with pytest.raises(StackBusy):
                await rig.fresh().actions.start(ADMIN, ENV_B, STACK)

    [refusal] = await rig.rows("stack_started")
    stack = present(await rig.db.fetch_one("SELECT last_operation FROM stack"))
    assert refusal["detail"]["reason"] == "compose_busy"
    assert stack["last_operation"] == "deploy"
    assert len(rig.engine.locks) == 0


async def test_an_update_waits_for_a_compose_slot_for_a_bounded_time(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await rig.services.actions.create(ADMIN, ENV_B, name=STACK, compose_text=PLAIN_STACK)
    rig.sim = await use_compose_limits(rig.compose, rig.host, operation_timeout=20.0, slots=1)
    waits: list[float | None] = []
    real_slot = rig.sim.slot

    def brief(wait: float | None) -> object:
        waits.append(wait)
        return real_slot(None if wait is None else SLOT_WAIT)

    monkeypatch.setattr(rig.sim, "slot", brief)

    async with real_slot(None):
        async with asyncio.timeout(10):
            with pytest.raises(StackBusy):
                await rig.fresh().actions.update(
                    ADMIN,
                    ENV_B,
                    STACK,
                    expected_file_digest=await stored_file_digest(rig, ADMIN, ENV_B, STACK),
                    compose_text=PLAIN_STACK + "# changed\n",
                )

    assert waits == [CONFIG_SLOT_WAIT_SECONDS]
    assert len(rig.engine.locks) == 0


async def test_the_running_count_includes_jobs_running_and_operations_about_to_start(
    registry: StackJobRegistry,
) -> None:
    gate = asyncio.Event()
    empty = registry.running_count()
    first = begin(registry, ADMIN, blocked_by(gate))
    with registry.reserve(STANDARD):
        counted = registry.running_count()
    gate.set()
    await ended(registry, first)

    assert (empty, counted, registry.running_count()) == (0, 2, 0)
