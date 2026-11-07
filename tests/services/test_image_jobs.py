"""Pull and push jobs: progress vocabulary, limits, ownership, cancellation and shutdown."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.docker_engine.models import ProgressDetail, PullProgress, PushProgress
from app.services import activity
from app.services.activity import AuditState, UserActor, current_audit
from app.services.assembly import start_pull_job, start_push_job
from app.services.container_provisioning import ConfigurationRejected
from app.services.image_bindings import RebindRefused
from app.services.image_jobs import (
    JOB_ID_PATTERN,
    MAX_JOBS_PER_USER,
    MAX_TRACKED_LAYERS,
    ImageJobRegistry,
    JobCancelled,
    JobKind,
    JobOutcome,
    JobProgress,
    JobResult,
    JobView,
    TooManyJobs,
    outcome_of,
)
from app.services.image_views import (
    ImageChangedSinceReview,
    ImageGone,
    ImageHasDependents,
    ImageInUse,
    ImagePullFailed,
    ImagePushFailed,
)
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import LockHolder, ObjectBusy
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable
from tests.access_support import ADMIN, ENV_A, MEMBER, STANDARD
from tests.auth_support import STANDARD_ID, FakeClock
from tests.docker_engine.container_engine import ENGINE_TEXT
from tests.image_support import (
    HUB_REFERENCE,
    REFERENCE,
    REFERENCE_KEY,
    binding_rows,
    decode_auth,
    full_id,
    image_id,
    register,
    rows_for,
)
from tests.registry_support import SECRET
from tests.services.conftest import ImageRig

LAYER = "a1b2c3d4e5f6"
OTHER_LAYER = "0f0e0d0c0b0a"
ONE_MIB = 1024 * 1024
PUSHED = 8

type Run = Callable[[JobProgress], Awaitable[JobResult]]


def pulled(
    status: str, layer: str | None = LAYER, current: int = 0, total: int = 0
) -> PullProgress:
    return PullProgress.model_validate(
        {"status": status, "id": layer, "progressDetail": {"current": current, "total": total}}
    )


async def instantly(progress: JobProgress) -> JobResult:
    return JobResult("done", image_id(1), "created")


def blocked_on(gate: asyncio.Event) -> Run:
    async def run(progress: JobProgress) -> JobResult:
        await gate.wait()
        return JobResult("done")

    return run


def failing(error: Exception) -> Run:
    async def run(progress: JobProgress) -> JobResult:
        raise error

    return run


async def settle(registry: ImageJobRegistry, job_id: str) -> None:
    task = registry._jobs[job_id].task
    assert task is not None
    await asyncio.wait({task})


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        ("Pulling fs layer", "queued"),
        ("Waiting", "waiting"),
        ("Downloading", "downloading"),
        ("Verifying Checksum", "verifying"),
        ("Download complete", "downloaded"),
        ("Extracting", "extracting"),
        ("Pull complete", "complete"),
        ("Already exists", "exists"),
        ("Layer already exists", "exists"),
        ("Preparing", "preparing"),
        ("Pushing", "pushing"),
        ("Pushed", "pushed"),
        ("Mounted from library/busybox", "mounted"),
        ("Digest: sha256:abc", "working"),
        ("Status: Downloaded newer image", "working"),
        ("downloading", "working"),
    ],
)
def test_a_layer_status_is_mapped_by_exact_string_and_anything_else_is_working(
    status: str, stage: str
) -> None:
    progress = JobProgress()

    progress.on_pull(pulled(status))

    assert [(layer.layer, layer.stage) for layer in progress.snapshot().layers] == [(LAYER, stage)]


def test_the_engines_status_text_never_reaches_the_snapshot() -> None:
    progress = JobProgress()

    progress.on_pull(pulled(f"Downloading {ENGINE_TEXT}"))

    assert ENGINE_TEXT not in repr(progress.snapshot())


@pytest.mark.parametrize("layer", [None, "", "short", LAYER.upper(), LAYER + "0", "g" * 12])
def test_an_event_without_a_well_formed_layer_id_is_not_tracked(layer: str | None) -> None:
    progress = JobProgress()

    progress.on_pull(pulled("Downloading", layer))

    assert progress.snapshot().layers == ()
    assert progress.version == 0


def test_layers_past_the_128th_are_counted_but_not_tracked() -> None:
    progress = JobProgress()
    for number in range(MAX_TRACKED_LAYERS + 5):
        progress.on_pull(pulled("Waiting", f"{number:012x}"))

    snapshot = progress.snapshot()

    assert len(snapshot.layers) == MAX_TRACKED_LAYERS
    assert snapshot.layers_total == MAX_TRACKED_LAYERS + 5


def test_bytes_are_summed_over_layers_that_report_a_total_and_clamped_to_it() -> None:
    progress = JobProgress()
    progress.on_pull(pulled("Downloading", LAYER, current=50, total=100))
    progress.on_pull(pulled("Downloading", OTHER_LAYER, current=900, total=200))
    progress.on_pull(pulled("Waiting", "111111111111"))

    snapshot = progress.snapshot()

    assert (snapshot.current_bytes, snapshot.total_bytes, snapshot.percent) == (250, 300, 83)


def test_a_finished_layer_counts_as_fully_transferred() -> None:
    progress = JobProgress()
    progress.on_pull(pulled("Downloading", LAYER, current=40, total=100))
    progress.on_pull(pulled("Pull complete", LAYER))

    snapshot = progress.snapshot()

    assert (snapshot.layers_done, snapshot.current_bytes, snapshot.percent) == (1, 100, 100)


def test_progress_with_no_known_total_has_no_percentage() -> None:
    progress = JobProgress()
    progress.on_pull(pulled("Waiting"))

    assert progress.snapshot().percent is None


def test_a_push_event_is_tracked_like_a_pull_event() -> None:
    progress = JobProgress()

    progress.on_push(PushProgress.model_validate({"status": "Pushed", "id": LAYER}))

    assert progress.snapshot().layers_done == 1


def test_every_event_that_changes_a_layer_advances_the_version() -> None:
    progress = JobProgress()
    before = progress.version

    progress.on_pull(pulled("Downloading", current=1, total=2))

    assert progress.version == before + 1


def test_progress_detail_is_non_negative_whatever_the_engine_sent() -> None:
    assert ProgressDetail.model_validate({"current": -4, "total": "x"}).current == 0


@pytest.mark.parametrize(
    ("error", "reason", "ok_message"),
    [
        pytest.param(ImagePullFailed("pull_failed"), "pull_failed", None, id="pull-failed"),
        pytest.param(
            ImagePullFailed("not_found_or_denied"), "not_found_or_denied", None, id="denied"
        ),
        pytest.param(ImagePushFailed("push_failed"), "push_failed", None, id="push-failed"),
        pytest.param(EnvironmentUnavailable("unreachable"), "unreachable", None, id="unreachable"),
        pytest.param(
            EnvironmentUnavailable("outcome_unknown"), "outcome_unknown", None, id="unknown"
        ),
        pytest.param(
            ObjectBusy(LockHolder(UserActor(STANDARD_ID), "x", "pull")),
            "object_busy",
            None,
            id="busy",
        ),
        pytest.param(CredentialSetUnavailable("missing"), "credential_unavailable", None, id="u"),
        pytest.param(BindingCredentialMissing(), "credential_missing", None, id="missing"),
        pytest.param(CredentialChoiceRequired(()), "credential_choice_required", None, id="ask"),
        pytest.param(CredentialSetMismatch(), "credential_mismatch", None, id="mismatch"),
        pytest.param(RebindRefused(), "forbidden", None, id="rebind"),
        pytest.param(PermissionDenied("no"), "forbidden", None, id="permission"),
        pytest.param(ResourceNotFound("no"), "not_found", None, id="not-found"),
        pytest.param(ImageGone(), "image_gone", None, id="gone"),
        pytest.param(ImageChangedSinceReview(), "changed", None, id="changed"),
        pytest.param(ValidationFailed({"x": "bad"}), "invalid", None, id="invalid"),
        pytest.param(ImageInUse(2), "image_in_use", None, id="in-use"),
        pytest.param(ImageHasDependents(), "image_has_dependents", None, id="dependents"),
        pytest.param(ObjectTooLarge(), "too_large", None, id="too-large"),
        pytest.param(SelfProtected.container(), "self_protected", None, id="self-protected"),
        pytest.param(ContainerTooLarge(), "too_large", None, id="container-too-large"),
        pytest.param(
            ProductError("Nothing was changed."), "failed", None, id="other-product-error"
        ),
        pytest.param(RuntimeError(ENGINE_TEXT), "failed", None, id="unexpected"),
    ],
)
def test_the_error_that_ended_a_job_becomes_a_stable_reason_and_a_product_message(
    error: Exception, reason: str, ok_message: str | None
) -> None:
    outcome = outcome_of(error)

    assert (outcome.ok, outcome.reason) == (False, reason)
    assert ENGINE_TEXT not in outcome.message


def test_an_unexpected_error_is_reported_as_a_generic_failure_without_its_class_name() -> None:
    class UnexpectedEngineFault(Exception):
        pass

    outcome = outcome_of(UnexpectedEngineFault(ENGINE_TEXT))

    assert (outcome.reason, outcome.message) == ("failed", "The transfer failed.")


def test_a_permission_that_ended_a_job_after_the_plan_does_not_say_why() -> None:
    assert "no longer have permission" in outcome_of(PermissionDenied("hidden detail")).message
    assert "hidden detail" not in outcome_of(PermissionDenied("hidden detail")).message


async def test_a_started_job_has_an_unguessable_id_and_runs_to_a_successful_outcome() -> None:
    registry = ImageJobRegistry()

    job_id = registry.start("pull", ADMIN, ENV_A, "busybox:1", instantly)
    await settle(registry, job_id)

    view = registry.view(job_id, ADMIN)
    assert re.fullmatch(JOB_ID_PATTERN, job_id)
    assert (view.state, view.kind, view.reference, view.environment_id) == (
        "succeeded",
        "pull",
        "busybox:1",
        ENV_A,
    )
    assert view.outcome == JobOutcome(True, "done", None, image_id(1), "created")
    assert view.finished_at is not None


async def test_a_job_that_raises_is_failed_with_a_product_outcome_and_no_exception_text() -> None:
    registry = ImageJobRegistry()

    job_id = registry.start("pull", ADMIN, ENV_A, "x", failing(RuntimeError(ENGINE_TEXT)))
    await settle(registry, job_id)

    view = registry.view(job_id, ADMIN)
    assert view.state == "failed"
    assert ENGINE_TEXT not in repr(view)


async def test_a_running_job_reports_the_progress_it_has_collected() -> None:
    registry = ImageJobRegistry()
    gate = asyncio.Event()
    collected = asyncio.Event()

    async def run(progress: JobProgress) -> JobResult:
        progress.on_pull(pulled("Downloading", current=1, total=4))
        collected.set()
        await gate.wait()
        return JobResult("done")

    job_id = registry.start("pull", ADMIN, ENV_A, "x", run)
    await collected.wait()
    running = registry.view(job_id, ADMIN)
    gate.set()
    await settle(registry, job_id)

    finished = registry.view(job_id, ADMIN)
    assert (running.state, running.running, running.progress.percent) == ("running", True, 25)
    assert finished.version != running.version


async def test_a_third_job_for_one_user_is_refused_and_another_user_may_still_start() -> None:
    registry = ImageJobRegistry()
    gate = asyncio.Event()
    for _ in range(MAX_JOBS_PER_USER):
        registry.start("pull", STANDARD, ENV_A, "x", blocked_on(gate))

    with pytest.raises(TooManyJobs) as refused:
        registry.start("pull", STANDARD, ENV_A, "x", blocked_on(gate))
    other = registry.start("pull", MEMBER, ENV_A, "x", blocked_on(gate))
    gate.set()
    await settle(registry, other)

    assert refused.value.scope == "user"
    assert str(MAX_JOBS_PER_USER) in str(refused.value)


async def test_the_process_limit_keeps_its_last_slot_for_administrators() -> None:
    registry = ImageJobRegistry(per_process=2)
    gate = asyncio.Event()
    registry.start("pull", STANDARD, ENV_A, "x", blocked_on(gate))

    with pytest.raises(TooManyJobs) as standard_refused:
        registry.start("pull", MEMBER, ENV_A, "x", blocked_on(gate))
    registry.start("pull", ADMIN, ENV_A, "x", blocked_on(gate))
    with pytest.raises(TooManyJobs) as admin_refused:
        registry.start("pull", ADMIN, ENV_A, "x", blocked_on(gate))
    gate.set()

    assert (standard_refused.value.scope, admin_refused.value.scope) == ("process", "process")


async def test_a_finished_job_frees_its_slot() -> None:
    registry = ImageJobRegistry(per_user=1)
    first = registry.start("pull", STANDARD, ENV_A, "x", instantly)
    await settle(registry, first)

    second = registry.start("pull", STANDARD, ENV_A, "x", instantly)
    await settle(registry, second)

    assert registry.view(second, STANDARD).state == "succeeded"


async def test_a_job_is_visible_to_its_starter_and_administrators_and_to_nobody_else() -> None:
    registry = ImageJobRegistry()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", instantly)
    await settle(registry, job_id)

    assert registry.view(job_id, STANDARD).job_id == job_id
    assert registry.view(job_id, ADMIN).job_id == job_id
    with pytest.raises(ResourceNotFound):
        registry.view(job_id, MEMBER)
    with pytest.raises(ResourceNotFound):
        registry.cancel(job_id, MEMBER)
    with pytest.raises(ResourceNotFound):
        registry.view("A" * 22, ADMIN)


async def test_cancelling_a_running_job_ends_it_cancelled_with_an_unknown_outcome() -> None:
    registry = ImageJobRegistry()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", blocked_on(asyncio.Event()))
    await asyncio.sleep(0)

    registry.cancel(job_id, STANDARD)
    await settle(registry, job_id)

    view = registry.view(job_id, STANDARD)
    assert view.state == "cancelled"
    assert view.outcome is not None
    assert (view.outcome.ok, view.outcome.reason) == (False, "outcome_unknown")


async def test_cancelling_a_job_before_it_ever_ran_still_settles_it() -> None:
    registry = ImageJobRegistry()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", blocked_on(asyncio.Event()))

    registry.cancel(job_id, STANDARD)
    await settle(registry, job_id)

    assert registry.view(job_id, STANDARD).state == "cancelled"


async def test_cancelling_a_finished_job_changes_nothing() -> None:
    registry = ImageJobRegistry()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", instantly)
    await settle(registry, job_id)

    registry.cancel(job_id, STANDARD)

    assert registry.view(job_id, STANDARD).state == "succeeded"


async def test_a_finished_job_is_kept_for_fifteen_minutes_and_then_forgotten() -> None:
    clock = FakeClock()
    registry = ImageJobRegistry(clock)
    job_id = registry.start("pull", STANDARD, ENV_A, "x", instantly)
    await settle(registry, job_id)

    clock.advance(timedelta(minutes=14))
    kept = registry.view(job_id, STANDARD)
    clock.advance(timedelta(minutes=2))

    assert kept.state == "succeeded"
    with pytest.raises(ResourceNotFound):
        registry.view(job_id, STANDARD)


async def test_only_the_newest_finished_jobs_are_kept() -> None:
    clock = FakeClock()
    registry = ImageJobRegistry(clock, keep=2)
    ids = []
    for _ in range(4):
        job_id = registry.start("pull", ADMIN, ENV_A, "x", instantly)
        await settle(registry, job_id)
        clock.advance(timedelta(seconds=1))
        ids.append(job_id)

    registry.view(ids[-1], ADMIN)

    assert len(registry) == 2
    with pytest.raises(ResourceNotFound):
        registry.view(ids[0], ADMIN)


async def test_shutdown_cancels_running_jobs_waits_for_them_and_refuses_new_ones() -> None:
    registry = ImageJobRegistry()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", blocked_on(asyncio.Event()))
    await asyncio.sleep(0)

    await registry.aclose()

    assert registry.view(job_id, STANDARD).state == "cancelled"
    with pytest.raises(RuntimeError, match="closed"):
        registry.start("pull", STANDARD, ENV_A, "x", instantly)


async def finish(rig: ImageRig, job_id: str) -> JobView:
    await settle(rig.engine.image_jobs, job_id)
    return rig.engine.image_jobs.view(job_id, ADMIN)


async def test_a_pull_job_runs_the_audited_pull_on_a_session_of_its_own_and_binds_the_image(
    image_rig: ImageRig,
) -> None:
    chosen = await register(image_rig, "chosen", users=(STANDARD_ID,))
    image_rig.host.pull_id = full_id(1)
    image_rig.host.pull_lines = [
        {"status": "Downloading", "id": LAYER, "progressDetail": {"current": 5, "total": 10}},
        {"status": "Pull complete", "id": LAYER},
    ]
    plan = await image_rig.images.actions.prepare_pull(STANDARD, ENV_A, REFERENCE)

    job_id = start_pull_job(image_rig.app, STANDARD, ENV_A, plan)
    view = await finish(image_rig, job_id)

    [row] = await binding_rows(image_rig.db)
    sent = decode_auth(image_rig.host.calls("POST", "/images/create")[0].headers["x-registry-auth"])
    assert view.state == "succeeded"
    assert view.outcome is not None
    assert (view.outcome.image_id, view.outcome.binding) == (image_id(1), "created")
    assert (view.progress.layers_total, view.progress.layers_done) == (1, 1)
    assert (row["image_reference"], row["registry_credential_id"]) == (REFERENCE_KEY, chosen)
    assert sent["username"] == "user-chosen"


async def test_a_job_state_holds_no_credential_and_no_engine_text(image_rig: ImageRig) -> None:
    await register(image_rig, "chosen")
    image_rig.host.pull_status = 500
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, REFERENCE)

    job_id = start_pull_job(image_rig.app, ADMIN, ENV_A, plan)
    view = await finish(image_rig, job_id)

    header = image_rig.host.calls("POST", "/images/create")[0].headers["x-registry-auth"]
    assert view.state == "failed"
    assert (view.outcome.reason if view.outcome else None) == "pull_failed"
    assert SECRET not in repr(view)
    assert header not in repr(view)
    assert ENGINE_TEXT not in repr(view)


async def test_a_pull_job_for_a_user_whose_grant_was_withdrawn_meanwhile_fails_as_not_found(
    image_rig: ImageRig,
) -> None:
    plan = await image_rig.images.actions.prepare_pull(STANDARD, ENV_A, HUB_REFERENCE)
    await EnvironmentAccessRepository(image_rig.db).remove_user("environment", ENV_A, STANDARD_ID)

    job_id = start_pull_job(image_rig.app, STANDARD, ENV_A, plan)
    view = await finish(image_rig, job_id)

    assert image_rig.host.calls("POST", "/images/create") == []
    assert (view.state, view.outcome.reason if view.outcome else None) == ("failed", "not_found")


async def test_cancelling_a_pull_job_records_an_interrupted_row_and_writes_no_binding(
    image_rig: ImageRig,
) -> None:
    gate = image_rig.host.hold("pull")
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, HUB_REFERENCE)
    job_id = start_pull_job(image_rig.app, ADMIN, ENV_A, plan)
    await asyncio.wait_for(gate.entered.wait(), timeout=2)

    image_rig.engine.image_jobs.cancel(job_id, ADMIN)
    view = await finish(image_rig, job_id)

    [row] = await rows_for(image_rig.db, "image_pulled")
    assert view.state == "cancelled"
    assert await binding_rows(image_rig.db) == []
    assert (row["result"], row["detail"]["reason"], row["detail"]["interrupted"]) == (
        "failure",
        "outcome_unknown",
        True,
    )


async def test_shutting_down_with_a_pull_running_records_its_interruption_before_returning(
    image_rig: ImageRig,
) -> None:
    gate = image_rig.host.hold("pull")
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, HUB_REFERENCE)
    job_id = start_pull_job(image_rig.app, ADMIN, ENV_A, plan)
    await asyncio.wait_for(gate.entered.wait(), timeout=2)

    await image_rig.engine.image_jobs.aclose()

    [row] = await rows_for(image_rig.db, "image_pulled")
    assert image_rig.engine.image_jobs.view(job_id, ADMIN).state == "cancelled"
    assert row["detail"]["interrupted"] is True


async def test_a_third_transfer_for_one_user_is_refused_while_two_run(image_rig: ImageRig) -> None:
    image_rig.host.hold("pull")
    plans = [
        await image_rig.images.actions.prepare_pull(STANDARD, ENV_A, f"busybox:{n}")
        for n in (1, 2, 3)
    ]
    start_pull_job(image_rig.app, STANDARD, ENV_A, plans[0])
    start_pull_job(image_rig.app, STANDARD, ENV_A, plans[1])

    with pytest.raises(TooManyJobs):
        start_pull_job(image_rig.app, STANDARD, ENV_A, plans[2])


async def test_a_push_job_pushes_the_image_and_binds_the_reference(image_rig: ImageRig) -> None:
    target = "registry.example.com/myorg/app:1"
    image_rig.host.add_image(target, full_id(PUSHED))
    plan = await image_rig.images.actions.prepare_push(ADMIN, ENV_A, image_id(PUSHED), target, None)

    job_id = start_push_job(image_rig.app, ADMIN, ENV_A, plan)
    view = await finish(image_rig, job_id)

    assert view.state == "succeeded"
    assert view.kind == "push"
    assert view.outcome is not None
    assert (view.outcome.image_id, view.outcome.binding) == (image_id(PUSHED), "created")
    assert len(await binding_rows(image_rig.db)) == 1


async def test_starting_a_job_without_the_runtimes_is_refused(image_rig: ImageRig) -> None:
    plan = await image_rig.images.actions.prepare_pull(ADMIN, ENV_A, HUB_REFERENCE)
    app: Any = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(RuntimeError, match="not been started"):
        start_pull_job(app, ADMIN, ENV_A, plan)


async def test_the_running_count_drops_when_a_job_finishes() -> None:
    registry = ImageJobRegistry()
    gate = asyncio.Event()
    job_id = registry.start("pull", STANDARD, ENV_A, "x", blocked_on(gate))
    running = registry.running_count()

    gate.set()
    await settle(registry, job_id)

    assert (running, registry.running_count()) == (1, 0)


CONTAINER_KINDS = ("container_create", "container_recreate")
FRESH_CONTEXT_KINDS = (*CONTAINER_KINDS, "onboarding_review")
CANCELLED_MESSAGE = "The pull was cancelled. Nothing was created."


@pytest.mark.parametrize("kind", FRESH_CONTEXT_KINDS)
async def test_a_container_or_review_job_starts_in_a_context_with_no_audit_scope_open(
    kind: JobKind,
) -> None:
    registry = ImageJobRegistry()
    seen: list[bool] = []

    async def run(progress: JobProgress) -> JobResult:
        try:
            current_audit()
        except RuntimeError:
            seen.append(False)
        else:
            seen.append(True)
        return JobResult("done")

    token = activity._current.set(AuditState(None))
    try:
        job_id = registry.start(kind, STANDARD, ENV_A, "x", run)
    finally:
        activity._current.reset(token)
    await settle(registry, job_id)

    assert seen == [False]


@pytest.mark.parametrize("third", ["push", "container_recreate"])
async def test_the_user_limit_counts_pull_push_and_create_jobs_together(third: JobKind) -> None:
    registry = ImageJobRegistry()
    gate = asyncio.Event()
    registry.start("pull", STANDARD, ENV_A, "x", blocked_on(gate))
    registry.start("container_create", STANDARD, ENV_A, "x", blocked_on(gate))

    with pytest.raises(TooManyJobs) as refused:
        registry.start(third, STANDARD, ENV_A, "x", blocked_on(gate))
    gate.set()

    assert refused.value.scope == "user"


async def test_a_container_job_may_be_cancelled_only_while_it_pulls() -> None:
    registry = ImageJobRegistry()
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def run(progress: JobProgress) -> JobResult:
        entered.set()
        await gate.wait()
        return JobResult("done")

    job_id = registry.start("container_create", STANDARD, ENV_A, "x", run)
    progress = registry._jobs[job_id].progress
    await entered.wait()
    registry.cancel(job_id, STANDARD)
    before_pull = registry.view(job_id, STANDARD).state
    progress.on_stage("pull")
    progress.on_stage("pulled")
    registry.cancel(job_id, STANDARD)
    after_pull = registry.view(job_id, STANDARD).state
    gate.set()
    await settle(registry, job_id)

    assert (before_pull, after_pull) == ("running", "running")
    assert progress.cancel_requested is False
    assert registry.view(job_id, STANDARD).state == "succeeded"


async def test_cancelling_a_container_job_during_its_pull_asks_its_body_to_stop() -> None:
    registry = ImageJobRegistry()
    entered = asyncio.Event()

    async def run(progress: JobProgress) -> JobResult:
        progress.on_stage("pull")
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert progress.cancel_requested
            raise JobCancelled(CANCELLED_MESSAGE) from None
        return JobResult("done")

    job_id = registry.start("container_create", STANDARD, ENV_A, "x", run)
    await entered.wait()

    registry.cancel(job_id, STANDARD)
    await settle(registry, job_id)

    view = registry.view(job_id, STANDARD)
    assert view.state == "cancelled"
    assert view.outcome == JobOutcome(False, CANCELLED_MESSAGE, "cancelled")
    assert view.progress.stages == ("pull",)


async def test_a_container_job_keeps_the_engine_text_only_for_an_administrator_starter() -> None:
    registry = ImageJobRegistry()
    error = ConfigurationRejected(ENGINE_TEXT)

    by_administrator = registry.start("container_create", ADMIN, ENV_A, "x", failing(error))
    by_standard = registry.start("container_create", STANDARD, ENV_A, "x", failing(error))
    await settle(registry, by_administrator)
    await settle(registry, by_standard)

    shown = registry.view(by_administrator, ADMIN).outcome
    hidden = registry.view(by_standard, ADMIN).outcome
    assert shown is not None and hidden is not None
    assert (shown.engine_detail, hidden.engine_detail) == (ENGINE_TEXT, None)
    assert ENGINE_TEXT not in shown.message and ENGINE_TEXT not in hidden.message


async def test_a_container_job_reports_the_container_and_the_flags_of_its_result() -> None:
    registry = ImageJobRegistry()

    async def run(progress: JobProgress) -> JobResult:
        progress.on_stage("create")
        return JobResult("Created web.", container_id="c" * 64, flags={"created": "started"})

    job_id = registry.start("container_create", STANDARD, ENV_A, "x", run)
    await settle(registry, job_id)

    view = registry.view(job_id, STANDARD)
    assert view.outcome is not None
    assert (view.outcome.container_id, dict(view.outcome.flags)) == (
        "c" * 64,
        {"created": "started"},
    )
    assert view.progress.stages == ("create",)
