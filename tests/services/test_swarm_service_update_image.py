"""Automated image updates of a Swarm service: what is sent, how it is watched, rolled back."""

import asyncio
import copy
from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr

from app.scheduler.model import RunSkipped
from app.scheduler.suppression import SuppressionRegistry
from app.services.acting import AutomationAuthority
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import PullCredentials
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.swarm_service_base import rollout_bound
from app.services.swarm_service_views import (
    ServiceChangedSinceReview,
    ServiceInvalid,
    ServiceUpdateFailed,
    ServiceVerifyPolicy,
)
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.automation_support import LOCK_WAIT, authority_context, event_rows
from tests.docker_engine.swarm_engine import plain_spec
from tests.image_support import REFERENCE, REFERENCE_KEY, digest, register
from tests.registry_support import bind_image
from tests.services.conftest import SwarmRig
from tests.services.test_swarm_service_reach import PIN
from tests.support import present
from tests.swarm_support import EMPTY_AUTH
from tests.swarm_updater_support import (
    ENGINE_TEXT,
    Rollout,
    Sleeper,
    after_update,
    crashed,
    inject_sleeper,
    paused,
    set_state,
    still_updating,
)
from tests.updater_support import UPDATER

OLD = digest(1)
NEW = digest(2)
OLD_IMAGE = f"{REFERENCE}@{OLD}"
NEW_IMAGE = f"{REFERENCE}@{NEW}"
AUTH = "c2VjcmV0LWF1dGg="
SECOND_PASS = 2
SHORT_WAIT = 0.01
WINDOW = 4
NO_WINDOW = 0
ONE_NANOSECOND_SECOND = 1_000_000_000
UPDATE_CONFIG = {"Parallelism": 1, "FailureAction": "pause", "Monitor": 2 * ONE_NANOSECOND_SECOND}
CURRENT_STARTED_AT = "2026-06-01T00:00:00Z"
OTHER_IMAGE = f"{REFERENCE}@{digest(9)}"


def completed(host: Any, service: dict[str, Any]) -> None:
    set_state(service, "completed", StartedAt=CURRENT_STARTED_AT)


class Setup:
    """A service, the authority that updates it and the sleeper that stands for waiting."""

    def __init__(
        self, swarm: SwarmRig, sid: str, authority: AutomationAuthority, sleep: Sleeper
    ) -> None:
        self.swarm = swarm
        self.sid = sid
        self.authority = authority
        self.sleep = sleep
        self.set_id = 0
        self.credentials = PullCredentials(None, SecretStr(AUTH), "binding")

    @property
    def host(self) -> Any:
        return self.swarm.host

    @property
    def service(self) -> dict[str, Any]:
        return self.swarm.host.services[self.sid]

    @property
    def version(self) -> int:
        return int(self.service["Version"]["Index"])

    @property
    def image(self) -> str:
        return str(self.service["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"])

    def actions(self) -> Any:
        return self.swarm.fresh().service_actions

    async def update(
        self,
        *,
        seconds: int = WINDOW,
        digest_: str = NEW,
        version: int | None = None,
        reference: str = REFERENCE,
        credentials: PullCredentials | None = None,
        authority: AutomationAuthority | None = None,
    ) -> Any:
        return await self.actions().update_image(
            authority or self.authority,
            ENV_A,
            self.sid,
            reference=reference,
            remote_digest=digest_,
            credentials=credentials or self.credentials,
            expected_version=self.version if version is None else version,
            verify=ServiceVerifyPolicy(seconds=seconds),
            note={"trigger": "update", "reference": reference},
        )

    async def rows(self, action: str = "service_updated") -> list[dict[str, Any]]:
        return await event_rows(self.swarm.db, action)

    def sent(self) -> list[str]:
        return [entry for entry in self.host.mutating() if "/update" in entry]


async def prepare(
    swarm: SwarmRig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    image: str = OLD_IMAGE,
    sleep: Any = None,
    bound: bool = True,
    replicas: int = 2,
    lock_wait: float = LOCK_WAIT,
    suppressions: SuppressionRegistry | None = None,
    **spec: Any,
) -> Setup:
    sleeper = inject_sleeper(monkeypatch, sleep)
    extra = {"UpdateConfig": UPDATE_CONFIG, **spec}
    sid = swarm.host.add_service(plain_spec("web", image=image, replicas=replicas, **extra))
    context = await authority_context(
        swarm.db,
        swarm.clock,
        swarm.engine.locks,
        environment_id=ENV_A,
        subsystem=UPDATER,
        lock_wait=lock_wait,
        suppressions=suppressions,
    )
    built = Setup(swarm, sid, context.authority(), sleeper)  # type: ignore[arg-type]
    if bound:
        built.set_id = await register(swarm, "main")
        await bind_image(swarm.db, built.set_id, ENV_A, REFERENCE_KEY)
        built.credentials = PullCredentials(built.set_id, SecretStr(AUTH), "binding")
    return built


async def test_an_update_changes_only_the_image_and_sends_the_bound_sets_header(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, Labels={"keep": "me"})
    built.service["Spec"]["TaskTemplate"]["ForceUpdate"] = 3
    before = copy.deepcopy(built.service["Spec"])
    after_update(built.host, completed)

    outcome = await built.update(seconds=NO_WINDOW)

    spec = built.service["Spec"]
    expected = copy.deepcopy(before)
    expected["TaskTemplate"]["ContainerSpec"]["Image"] = NEW_IMAGE
    assert spec == expected
    assert built.host.auth_headers[-1] == ("update", AUTH)
    assert built.host.registry_auth_from[-1] is None
    assert (outcome.verified, outcome.update_state) == (True, "completed")
    [row] = await built.rows()
    detail = row["detail"]
    assert (row["result"], row["actor_type"], row["actor_subsystem"]) == (
        "success",
        "subsystem",
        UPDATER,
    )
    assert row["rule_id"] is not None and row["job_run_id"] is not None
    assert (detail["image_before"], detail["image_after"]) == (OLD_IMAGE, NEW_IMAGE)
    assert (detail["digest_before"], detail["digest_after"]) == (OLD, NEW)
    assert (detail["changed"], detail["image_changed"], detail["digest_pinned"]) == (
        "image",
        True,
        True,
    )
    assert (detail["credential_set_id"], detail["anonymous"], detail["credential_source"]) == (
        built.set_id,
        False,
        "binding",
    )
    assert (detail["failure_action"], detail["verify_seconds"], detail["verified"]) == (
        "pause",
        NO_WINDOW,
        True,
    )
    assert (detail["tasks_running_before"], detail["trigger"]) == (2, "update")
    assert detail["binding"] == "kept"


async def test_an_anonymous_binding_sends_the_empty_auth_document_never_a_stored_credential(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, bound=False)
    anonymous = PullCredentials(None, None, "binding")

    await built.update(seconds=NO_WINDOW, credentials=anonymous)

    assert built.host.auth_headers[-1] == ("update", EMPTY_AUTH)
    assert built.host.registry_auth_from[-1] is None
    [row] = await built.rows()
    assert (row["detail"]["anonymous"], row["detail"]["credential_set_id"]) == (True, None)


async def test_a_spec_already_at_the_digest_sends_nothing_and_leaves_no_row(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)

    outcome = await built.update(digest_=OLD)

    assert (outcome.verified, outcome.version) == (None, None)
    assert built.sent() == [] and built.image == OLD_IMAGE
    assert await built.rows() == []


@pytest.mark.parametrize(
    ("image", "stale", "reference"),
    [
        (OLD_IMAGE, True, REFERENCE),
        (f"{REFERENCE.rsplit(':', 1)[0]}:2@{OLD}", False, REFERENCE),
        (REFERENCE, False, REFERENCE),
        (f"{REFERENCE.rsplit(':', 1)[0]}@{OLD}", False, REFERENCE),
        ("a" * 12, False, REFERENCE),
    ],
    ids=["stale-version", "reference-moved", "unpinned", "digest-only", "image-id"],
)
async def test_an_update_the_reviewed_state_no_longer_describes_is_refused_before_any_write(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch, image: str, stale: bool, reference: str
) -> None:
    built = await prepare(swarm, monkeypatch, image=image)
    version = built.version - 1 if stale else built.version

    with pytest.raises(ServiceChangedSinceReview):
        await built.update(version=version, reference=reference)

    assert built.sent() == [] and built.image == image
    [row] = await built.rows()
    assert (row["result"], row["detail"]["reason"]) == ("failure", "changed_since_review")


async def test_arguments_an_automation_run_may_not_pass_are_refused_before_the_service_is_read(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    not_binding = PullCredentials(built.set_id, SecretStr(AUTH), "no_match")

    with pytest.raises(ValueError):
        await built.update(credentials=not_binding)
    with pytest.raises(ValueError):
        await built.update(digest_="sha256:short")
    with pytest.raises(TypeError):
        await built.actions().update_image(
            ADMIN,
            ENV_A,
            built.sid,
            reference=REFERENCE,
            remote_digest=NEW,
            credentials=built.credentials,
            expected_version=built.version,
            verify=ServiceVerifyPolicy(seconds=NO_WINDOW),
        )

    assert built.sent() == []


async def test_a_set_the_owner_may_not_use_refuses_the_update_before_anything_is_sent(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    asked: list[int] = []

    async def refuse(set_id: int) -> None:
        asked.append(set_id)
        raise CredentialSetUnavailable("not_granted_user")

    refusing = replace(built.authority, authorise_credential_set=refuse)

    with pytest.raises(CredentialSetUnavailable):
        await built.update(authority=refusing)

    assert asked == [built.set_id] and built.sent() == []
    [row] = await built.rows()
    assert (row["result"], row["detail"]["reason"]) == ("failure", "credential_unavailable")


async def test_a_standard_owner_is_refused_a_restricted_spec_and_an_administrator_is_not(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounts = [{"Type": "bind", "Source": "/etc", "Target": "/x"}]
    built = await prepare(swarm, monkeypatch)
    built.service["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"] = mounts
    built.service["Version"]["Index"] += 1
    standard = replace(built.authority, owner_is_administrator=False)
    spec_before = copy.deepcopy(built.service["Spec"])

    with pytest.raises(ServiceInvalid) as refused:
        await built.update(authority=standard)

    assert refused.value.reason == "restricted" and built.service["Spec"] == spec_before
    [row] = await built.rows()
    assert (row["result"], row["detail"]["reason"]) == ("failure", "restricted")
    await built.update(seconds=NO_WINDOW)
    assert built.image == NEW_IMAGE


async def test_a_service_another_user_holds_refuses_the_update_without_a_row(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, lock_wait=SHORT_WAIT)
    holder = LockHolder(UserActor.of(ADMIN), "someone", "scale")

    async with swarm.engine.locks.hold(ObjectKey(ENV_A, "service", built.sid), holder):
        with pytest.raises(ObjectBusy):
            await built.update()

    assert await built.rows() == [] and built.sent() == []


async def test_a_service_a_user_just_acted_on_is_suppressed_and_writes_no_row(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    suppressions = SuppressionRegistry(swarm.clock)
    built = await prepare(swarm, monkeypatch, suppressions=suppressions)
    suppressions.note(ObjectKey(ENV_A, "service", built.sid))

    with pytest.raises(RunSkipped):
        await built.update()

    assert await built.rows() == [] and built.sent() == []


SUCCESSES = [
    ("rollout-completed", 2, 2 * ONE_NANOSECOND_SECOND, 8, "completed", 58, [2.0] * 4),
    ("rollout-completed-default-monitor", 2, None, 10, "completed", 86, [2.0] * 5),
    ("rollout-global", 2, 2 * ONE_NANOSECOND_SECOND, 8, "completed", 558, [2.0] * 4),
    ("rollout-zero-replicas", 0, 2 * ONE_NANOSECOND_SECOND, 0, None, 353, []),
]
FAILURES = [
    ("rollout-paused", "update_paused", "paused", 119, 4),
    ("rollout-paused-bad-image", "update_paused", "paused", 292, 2),
    ("rollout-rolled-back-by-swarm", "rolled_back_by_swarm", "rollback_completed", 218, 12),
    ("rollout-continue-crashing", "tasks_not_running", "completed", 259, 6),
]


async def replay(
    swarm: SwarmRig,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    *,
    replicas: int,
    monitor: int | None,
    global_mode: bool = False,
) -> tuple[Setup, Rollout]:
    config = {"Parallelism": 1, "FailureAction": "pause"}
    if monitor is not None:
        config["Monitor"] = monitor
    built = await prepare(swarm, monkeypatch, replicas=replicas, UpdateConfig=config)
    if global_mode:
        built.service["Spec"]["Mode"] = {"Global": {}}
    desired = 2 if global_mode else replicas
    rollout = Rollout(built.host, built.sid, shape, desired)
    rollout.install()
    inject_sleeper(monkeypatch, rollout.sleep)
    return built, rollout


@pytest.mark.parametrize(
    ("shape", "replicas", "monitor", "seconds", "state", "version", "pauses"),
    SUCCESSES,
    ids=[row[0] for row in SUCCESSES],
)
async def test_a_rollout_the_engine_completed_is_verified_after_the_waits_it_took(
    swarm: SwarmRig,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    replicas: int,
    monitor: int | None,
    seconds: int,
    state: str | None,
    version: int,
    pauses: list[float],
) -> None:
    built, rollout = await replay(
        swarm, monkeypatch, shape, replicas=replicas, monitor=monitor, global_mode="global" in shape
    )

    outcome = await built.update(seconds=seconds)

    assert (outcome.verified, outcome.update_state, outcome.version) == (True, state, version)
    assert rollout.slept == pauses
    [row] = await built.rows()
    assert (row["result"], row["detail"]["verified"]) == ("success", True)


@pytest.mark.parametrize(
    ("shape", "reason", "state", "version", "seconds"),
    FAILURES,
    ids=[row[0] for row in FAILURES],
)
async def test_a_rollout_that_pauses_rolls_back_crashes_or_leaves_tasks_down_fails_with_its_version(
    swarm: SwarmRig,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    reason: str,
    state: str,
    version: int,
    seconds: int,
) -> None:
    built, rollout = await replay(
        swarm, monkeypatch, shape, replicas=2, monitor=2 * ONE_NANOSECOND_SECOND
    )

    with pytest.raises(ServiceUpdateFailed) as failed:
        await built.update(seconds=30)

    assert (failed.value.reason, failed.value.update_state, failed.value.version) == (
        reason,
        state,
        version,
    )
    assert sum(rollout.slept) == seconds
    [row] = await built.rows()
    assert row["result"] == "failure"
    assert (row["detail"]["reason"], row["detail"]["verify_failure"]) == (reason, reason)
    assert row["detail"]["update_state"] == state


async def test_a_rollout_still_updating_at_the_bound_is_a_success_that_is_not_verified(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, UpdateConfig={"Parallelism": 1})
    bound = rollout_bound(built.service["Spec"], 2)
    original = built.host._service_update

    def updating(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        still_updating(built.host, service)
        return reply

    built.host._service_update = updating

    outcome = await built.update(seconds=WINDOW)

    assert (outcome.verified, outcome.update_state) == (False, "updating")
    assert sum(built.sleep.slept) == bound
    [row] = await built.rows()
    assert (row["result"], row["detail"]["verify_failure"]) == ("success", "still_updating")
    assert row["detail"]["verified"] is False


async def test_the_engines_own_text_never_reaches_the_row(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    original = built.host._service_update

    def failing(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        paused(built.host, service)
        return reply

    built.host._service_update = failing

    with pytest.raises(ServiceUpdateFailed):
        await built.update()

    [row] = await built.rows()
    assert ENGINE_TEXT not in str(row)


HOSTILE = [
    ("unknown-state", lambda host, service: set_state(service, "surprising"), "unverifiable"),
    ("wrong-id", lambda host, service: service.__setitem__("ID", "z" * 25), "unverifiable"),
    ("vanished", lambda host, service: host.services.pop(service["ID"]), "service_gone"),
    ("outage", lambda host, service: host.outage.add("services"), "unverifiable"),
]


@pytest.mark.parametrize(
    ("step", "reason"),
    [(step, reason) for _, step, reason in HOSTILE],
    ids=[n for n, _, _ in HOSTILE],
)
async def test_an_answer_the_watch_cannot_trust_is_unverifiable_and_a_missing_service_is_gone(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch, step: Any, reason: str
) -> None:
    built = await prepare(swarm, monkeypatch)
    after_update(built.host, step)

    with pytest.raises(ServiceUpdateFailed) as failed:
        await built.update()

    assert failed.value.reason == reason
    [row] = await built.rows()
    assert (row["result"], row["detail"]["reason"]) == ("failure", reason)
    assert len(built.sent()) == 1


async def test_a_completed_status_from_an_earlier_rollout_is_not_taken_for_this_one(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    built.service["UpdateStatus"] = {"State": "completed", "StartedAt": "T0"}

    stale = await built.update(seconds=NO_WINDOW)

    assert stale.verified is False
    [row] = await built.rows()
    assert (row["result"], row["detail"]["verify_failure"]) == ("success", "not_observed")


async def test_a_completed_status_that_started_after_the_send_is_this_rollout(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    built.service["UpdateStatus"] = {"State": "completed", "StartedAt": "T0"}
    original = built.host._service_update

    def restarted(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        service["UpdateStatus"] = {"State": "completed", "StartedAt": "T1"}
        return reply

    built.host._service_update = restarted

    outcome = await built.update(seconds=NO_WINDOW)

    assert outcome.verified is True


async def test_a_stale_completed_status_that_gives_way_to_updating_is_followed_to_completion(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    states = iter(["updating", "completed"])

    async def sleep(seconds: float) -> None:
        set_state(built.service, next(states), StartedAt="T1")

    built = await prepare(swarm, monkeypatch, sleep=sleep)
    built.service["UpdateStatus"] = {"State": "completed", "StartedAt": "T0"}

    outcome = await built.update(seconds=WINDOW)

    assert outcome.verified is True


async def test_a_stale_completed_status_for_the_whole_window_stays_unverified(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    built.service["UpdateStatus"] = {"State": "completed", "StartedAt": "T0"}

    outcome = await built.update(seconds=WINDOW)

    assert outcome.verified is False and built.sleep.slept == [2.0, 2.0]


async def test_a_service_with_no_status_after_the_send_is_decided_by_its_tasks(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)

    outcome = await built.update(seconds=NO_WINDOW)

    assert (outcome.verified, outcome.update_state) == (True, None)


async def test_a_cancelled_watch_leaves_a_failure_row_and_no_binding(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def interrupted(seconds: float) -> None:
        raise asyncio.CancelledError

    built = await prepare(swarm, monkeypatch, sleep=interrupted)
    built.service["UpdateStatus"] = {"State": "updating", "StartedAt": "T0"}
    before = await bindings(swarm)

    with pytest.raises(asyncio.CancelledError):
        await built.update()

    [row] = await built.rows()
    assert row["result"] == "failure"
    assert (row["detail"]["reason"], row["detail"]["interrupted"]) == ("interrupted", True)
    assert row["detail"]["verify_failure"] == "interrupted" and "binding" not in row["detail"]
    assert (row["actor_type"], row["actor_subsystem"]) == ("subsystem", UPDATER)
    assert await bindings(swarm) == before


async def test_a_completed_update_records_the_digest_it_sent_on_the_binding(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    before = await bindings(swarm)

    await built.update(seconds=NO_WINDOW)

    after = await bindings(swarm)
    assert before != after and after[0][1] == NEW


async def bindings(swarm: SwarmRig) -> list[tuple[str, str | None]]:
    rows = await swarm.db.fetch_limited(
        "SELECT image_reference, observed_digest FROM image_credential_binding ORDER BY id",
        (),
        limit=50,
    )
    return [(row["image_reference"], row["observed_digest"]) for row in rows]


@pytest.mark.parametrize(
    ("update_config", "running", "global_mode", "bound"),
    [
        ({"Parallelism": 1, "Monitor": 2 * ONE_NANOSECOND_SECOND}, 2, False, 64.0),
        ({"Parallelism": 1}, 2, False, 70.0),
        ({"Parallelism": 2, "Monitor": 5 * ONE_NANOSECOND_SECOND}, 5, False, 105.0),
        ({"Parallelism": 0, "Monitor": 0}, 2, False, 60.0),
        ({"Parallelism": 1, "Monitor": -1}, 2, False, 70.0),
        ({"Parallelism": 1, "Delay": 10 * ONE_NANOSECOND_SECOND}, 2, False, 90.0),
        ({"Parallelism": 1, "Monitor": 100 * ONE_NANOSECOND_SECOND}, 2, False, 260.0),
        ({"Parallelism": 1, "Monitor": 300 * ONE_NANOSECOND_SECOND}, 2, False, 300.0),
        ({"Parallelism": 1, "Monitor": 2 * ONE_NANOSECOND_SECOND}, 3, True, 96.0),
        (None, 2, False, 70.0),
    ],
    ids=[
        "monitor-set",
        "monitor-default",
        "parallel-batches",
        "no-parallelism-no-monitor",
        "negative-monitor",
        "delay-counted",
        "long-monitor",
        "capped",
        "global-counts-running-tasks",
        "no-update-config",
    ],
)
def test_the_rollout_bound_is_the_batches_times_monitor_delay_and_allowance_capped(
    update_config: dict[str, int] | None, running: int, global_mode: bool, bound: float
) -> None:
    spec: dict[str, Any] = {
        "Mode": {"Global": {}} if global_mode else {"Replicated": {"Replicas": running}}
    }
    if update_config is not None:
        spec["UpdateConfig"] = update_config

    assert rollout_bound(spec, running) == bound


@pytest.mark.parametrize(
    "arguments", [{"seconds": -1}, {"seconds": 301}, {"seconds": 1, "interval": 0}]
)
def test_a_verify_policy_outside_the_bounded_loop_is_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ServiceVerifyPolicy(**arguments)


async def test_a_completed_rollout_whose_tasks_crashed_fails_tasks_not_running(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    original = built.host._service_update

    def crash(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        crashed(built.host, service)
        return reply

    built.host._service_update = crash

    with pytest.raises(ServiceUpdateFailed) as failed:
        await built.update(seconds=WINDOW + SECOND_PASS)

    assert (failed.value.reason, failed.value.update_state) == ("tasks_not_running", "completed")
    [row] = await built.rows()
    assert row["detail"]["tasks_running_after"] == 0
    assert row["detail"]["tasks_running_before"] == 2


async def broken_service(swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch) -> tuple[Setup, int]:
    """A service whose update paused, with the version the failed watch read."""
    built = await prepare(swarm, monkeypatch)
    original = built.host._service_update

    def pause(service: Any, query: Any, body: Any, headers: Any) -> Any:
        reply = original(service, query, body, headers)
        paused(built.host, service)
        return reply

    built.host._service_update = pause
    with pytest.raises(ServiceUpdateFailed) as failed:
        await built.update()
    built.host._service_update = original
    assert failed.value.version is not None
    return built, failed.value.version


def images_of(built: Setup) -> tuple[str, str]:
    document = built.service
    return (
        document["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"],
        document["PreviousSpec"]["TaskTemplate"]["ContainerSpec"]["Image"],
    )


async def rollback(
    built: Setup,
    version: int,
    *,
    authority: AutomationAuthority | None = None,
    current: str | None = None,
    previous: str | None = None,
) -> Any:
    now, before = images_of(built)
    return await built.actions().rollback(
        authority or built.authority,
        ENV_A,
        built.sid,
        expected_version=version,
        note={"trigger": "update", "rollback_reason": "update_paused"},
        expected_image=current or now,
        expected_previous_image=previous or before,
    )


async def test_an_automated_rollback_with_the_version_the_watch_read_restores_the_previous_image(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, version = await broken_service(swarm, monkeypatch)

    await rollback(built, version)

    assert built.image == OLD_IMAGE
    assert built.host.registry_auth_from[-1] == "previous-spec"
    [row] = await built.rows("service_rolled_back")
    assert (row["result"], row["actor_subsystem"], row["detail"]["trigger"]) == (
        "success",
        UPDATER,
        "update",
    )
    assert (row["detail"]["image_before"], row["detail"]["image_after"]) == (
        NEW_IMAGE,
        OLD_IMAGE,
    )


@pytest.mark.parametrize("which", ["current", "previous"])
async def test_a_rollback_that_would_undo_other_images_than_it_names_is_changed_since_review(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    built, version = await broken_service(swarm, monkeypatch)
    sent = len(built.host.registry_auth_from)

    with pytest.raises(ServiceChangedSinceReview):
        options: dict[str, Any] = {which: OTHER_IMAGE}
        await rollback(built, version, **options)

    assert len(built.host.registry_auth_from) == sent
    [row] = await built.rows("service_rolled_back")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "changed_since_review")


async def test_a_rollback_after_the_service_moved_on_is_changed_since_review(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, version = await broken_service(swarm, monkeypatch)
    built.service["Version"]["Index"] += 1

    with pytest.raises(ServiceChangedSinceReview):
        await rollback(built, version)

    assert built.image == NEW_IMAGE


async def test_the_images_and_the_note_travel_together_and_only_with_an_automation_run(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, version = await broken_service(swarm, monkeypatch)
    now, before = images_of(built)
    actions = built.actions()

    with pytest.raises(ValueError):
        await actions.rollback(built.authority, ENV_A, built.sid, expected_version=version, note={})
    with pytest.raises(ValueError):
        await actions.rollback(
            built.authority, ENV_A, built.sid, expected_version=version, expected_image=now
        )
    with pytest.raises(ValueError):
        await actions.rollback(
            ADMIN,
            ENV_A,
            built.sid,
            expected_version=version,
            expected_image=now,
            expected_previous_image=before,
        )
    with pytest.raises(ValueError):
        await actions.rollback(ADMIN, ENV_A, built.sid, expected_version=version, note={})

    assert built.image == NEW_IMAGE


async def test_a_users_rollback_is_unchanged_by_the_automation_path(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, version = await broken_service(swarm, monkeypatch)

    await built.actions().rollback(ADMIN, ENV_A, built.sid, expected_version=version)

    assert built.image == OLD_IMAGE
    [row] = await built.rows("service_rolled_back")
    assert (row["actor_type"], row["actor_subsystem"]) == ("user", None)


async def plant_previous_volume(built: Setup, source: str) -> None:
    previous = copy.deepcopy(built.service["Spec"])
    previous["TaskTemplate"]["Placement"] = {"Constraints": [PIN]}
    previous["TaskTemplate"]["ContainerSpec"]["Mounts"] = [
        {"Type": "volume", "Source": source, "Target": "/d"}
    ]
    built.service["PreviousSpec"] = previous
    built.service["Version"]["Index"] += 1


async def test_a_standard_owner_is_refused_a_rollback_to_a_volume_they_cannot_write(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm.host.add_volume("data")
    sleeper = inject_sleeper(monkeypatch)
    sid = swarm.host.add_service(plain_spec("web", image=OLD_IMAGE, replicas=2))
    context = await authority_context(
        swarm.db,
        swarm.clock,
        swarm.engine.locks,
        environment_id=ENV_A,
        subsystem=UPDATER,
        allow=lambda key: key.object_type != "volume",
    )
    built = Setup(swarm, sid, replace(context.authority(), owner_is_administrator=False), sleeper)  # type: ignore[arg-type]
    await plant_previous_volume(built, "data")

    with pytest.raises(ServiceInvalid) as refused:
        await rollback(built, built.version)

    assert list(refused.value.fields) == ["mount-0-source"]
    assert any(key.object_type == "volume" for key in present(context.owner_gate).checked)  # type: ignore[attr-defined]
    assert built.image == OLD_IMAGE


async def test_a_standard_owner_who_may_write_the_volume_is_allowed_the_rollback(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm.host.add_volume("data")
    sleeper = inject_sleeper(monkeypatch)
    sid = swarm.host.add_service(plain_spec("web", image=OLD_IMAGE, replicas=2))
    context = await authority_context(
        swarm.db, swarm.clock, swarm.engine.locks, environment_id=ENV_A, subsystem=UPDATER
    )
    built = Setup(swarm, sid, replace(context.authority(), owner_is_administrator=False), sleeper)  # type: ignore[arg-type]
    await plant_previous_volume(built, "data")

    await rollback(built, built.version)

    mounts = built.service["Spec"]["TaskTemplate"]["ContainerSpec"]["Mounts"]
    assert mounts[0]["Source"] == "data"
    assert any(key.object_type == "volume" for key in present(context.owner_gate).checked)  # type: ignore[attr-defined]


async def test_a_standard_users_rollback_to_a_volume_they_cannot_write_is_still_refused(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm.host.add_volume("data")
    created = await swarm.sw.service_actions.create(
        STANDARD,
        ENV_A,
        {"name": "web", "image": "busybox:latest", "mode": "replicated", "replicas": "1"}
        | {"constraints": PIN},
    )
    swarm.fresh()
    service = swarm.host.services[created.service_id]
    previous = copy.deepcopy(service["Spec"])
    previous["TaskTemplate"]["Placement"] = {"Constraints": [PIN]}
    previous["TaskTemplate"]["ContainerSpec"]["Mounts"] = [
        {"Type": "volume", "Source": "data", "Target": "/d"}
    ]
    service["PreviousSpec"] = previous
    service["Version"]["Index"] += 1

    with pytest.raises(ServiceInvalid) as refused:
        await swarm.sw.service_actions.rollback(
            STANDARD, ENV_A, created.service_id, expected_version=service["Version"]["Index"]
        )

    assert list(refused.value.fields) == ["mount-0-source"]


def running_tasks(built: Setup) -> set[str]:
    return {i for i, t in built.host.tasks.items() if t["DesiredState"] == "running"}


async def refresh(built: Setup, **changes: Any) -> Any:
    return await built.actions().refresh_credentials(
        changes.pop("authority", built.authority),
        ENV_A,
        built.sid,
        reference=changes.pop("reference", REFERENCE),
        credentials=changes.pop("credentials", built.credentials),
        note={"trigger": "refresh"},
    )


async def test_a_refresh_sends_the_same_spec_with_the_header_and_replaces_no_task(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, Labels={"keep": "me"})
    spec = copy.deepcopy(built.service["Spec"])
    running = running_tasks(built)
    bound_before = await bindings(swarm)

    await refresh(built)

    assert built.service["Spec"] == spec
    assert built.service["PreviousSpec"] == spec
    assert running_tasks(built) == running
    assert built.host.auth_headers[-1] == ("update", AUTH)
    assert built.host.registry_auth_from[-1] is None
    [row] = await built.rows("service_credentials_refreshed")
    detail = row["detail"]
    assert (row["result"], row["actor_subsystem"], detail["trigger"]) == (
        "success",
        UPDATER,
        "refresh",
    )
    assert (detail["provider"], detail["reference"], detail["credential_set_id"]) == (
        "ecr",
        REFERENCE,
        built.set_id,
    )
    assert await bindings(swarm) == bound_before


@pytest.mark.parametrize("state", ["updating", "paused", "rollback_started", "rollback_paused"])
async def test_a_refresh_during_a_rollout_is_refused_and_sends_nothing(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    built = await prepare(swarm, monkeypatch)
    built.service["UpdateStatus"] = {"State": state}

    with pytest.raises(ServiceUpdateFailed) as failed:
        await refresh(built)

    assert failed.value.reason == "update_in_progress" and built.sent() == []
    [row] = await built.rows("service_credentials_refreshed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "update_in_progress")


async def test_a_refresh_for_a_reference_the_service_no_longer_runs_is_changed_since_review(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch, image=f"{REFERENCE.rsplit(':', 1)[0]}:2@{OLD}")

    with pytest.raises(ServiceChangedSinceReview):
        await refresh(built)

    assert built.sent() == []


async def test_a_refresh_with_credentials_that_are_not_the_bindings_is_refused(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)

    with pytest.raises(ValueError):
        await refresh(built, credentials=PullCredentials(built.set_id, None, "no_match"))

    assert built.sent() == []


async def test_a_refresh_whose_set_the_owner_may_not_use_is_refused_and_sends_nothing(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)

    async def refuse(set_id: int) -> None:
        raise CredentialSetUnavailable("not_granted_environment")

    with pytest.raises(CredentialSetUnavailable):
        await refresh(built, authority=replace(built.authority, authorise_credential_set=refuse))

    assert built.sent() == []


async def test_a_refresh_leaves_the_spec_untouched_when_a_legacy_key_changes_nothing(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    spec = built.service["Spec"]
    spec["TaskTemplate"]["Networks"] = [{"Target": "n1"}]
    spec["Networks"] = [{"Target": "n1"}]
    built.service["Version"]["Index"] += 1
    template = copy.deepcopy(spec["TaskTemplate"])

    await refresh(built)

    assert "Networks" not in built.service["Spec"]
    assert built.service["Spec"]["TaskTemplate"] == template


async def test_a_refresh_that_would_change_the_task_template_is_refused_and_sends_nothing(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = await prepare(swarm, monkeypatch)
    spec = built.service["Spec"]
    spec["Networks"] = [{"Target": "legacy"}]
    spec["TaskTemplate"].pop("Networks", None)
    built.service["Version"]["Index"] += 1

    with pytest.raises(ServiceUpdateFailed) as failed:
        await refresh(built)

    assert failed.value.reason == "legacy_networks" and built.sent() == []
    [row] = await built.rows("service_credentials_refreshed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "legacy_networks")


FAILURE_REASONS = [
    "update_paused",
    "tasks_not_running",
    "rolled_back_by_swarm",
    "rollback_paused",
    "service_gone",
    "unverifiable",
    "update_in_progress",
    "legacy_networks",
]


def test_every_update_failure_states_fixed_text_of_its_own_and_an_unknown_one_a_general_text() -> (
    None
):
    texts = [str(ServiceUpdateFailed(reason, None, None)) for reason in FAILURE_REASONS]
    general = str(ServiceUpdateFailed("surprising", None, None))

    assert len(set(texts)) == len(FAILURE_REASONS) and general not in texts
    assert general == "The service update failed."
