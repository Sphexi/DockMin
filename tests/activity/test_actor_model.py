"""The actor model: what each kind of actor records, and what the writer refuses."""

from dataclasses import dataclass

import pytest

from app.auth.principal import Principal
from app.logging import request_id_var
from app.services.activity import (
    ANONYMOUS_ACTIONS,
    SUBSYSTEMS,
    ActivityRecorder,
    AnonymousActor,
    SubsystemActor,
    UnknownSubsystem,
    UserActor,
    audited,
    current_audit,
)
from tests.activity.support import RecordingSession

USER_ID = 7
RULE_ID = 11
JOB_RUN_ID = 13
ACTION = "probe"
TARGET = "probe_target"
UNREGISTERED = "container-restarter"
LOGIN = "login"
REQUEST_ID = "0123456789abcdef"

REQUIRES_RULE = [name for name, needs_rule in SUBSYSTEMS.items() if needs_rule]
TAKES_NO_RULE = [name for name, needs_rule in SUBSYSTEMS.items() if not needs_rule]
WRONG_RULE_SHAPE = [
    *[(name, None) for name in REQUIRES_RULE],
    *[(name, RULE_ID) for name in TAKES_NO_RULE],
]
RULE_FOR = {name: RULE_ID if needs_rule else None for name, needs_rule in SUBSYSTEMS.items()}


async def record(recorder: ActivityRecorder, actor: object, **overrides: object) -> None:
    arguments = {"action": ACTION, "target_type": TARGET, "result": "success"} | overrides
    await recorder.record_event(actor, **arguments)  # type: ignore[arg-type]


async def test_a_user_action_records_the_user_and_no_subsystem(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    await record(pure_recorder, UserActor(USER_ID))

    [row] = recording.writes
    assert (row["actor_type"], row["actor_user_id"], row["category"]) == (
        "user",
        USER_ID,
        "user_action",
    )
    assert (row["actor_subsystem"], row["rule_id"], row["job_run_id"]) == (None, None, None)
    assert row["request_id"] is None


def test_a_principal_converts_to_a_user_actor_carrying_its_user_id() -> None:
    principal = Principal(USER_ID, "someone", "standard")

    assert UserActor.of(principal) == UserActor(USER_ID)


@pytest.mark.parametrize("name", sorted(SUBSYSTEMS))
async def test_a_subsystem_action_records_the_subsystem_and_rule_and_no_user(
    pure_recorder: ActivityRecorder, recording: RecordingSession, name: str
) -> None:
    await record(pure_recorder, SubsystemActor(name, RULE_FOR[name], JOB_RUN_ID))

    [row] = recording.writes
    assert (row["actor_type"], row["actor_subsystem"], row["category"]) == (
        "subsystem",
        name,
        "automation",
    )
    assert (row["rule_id"], row["job_run_id"], row["actor_user_id"]) == (
        RULE_FOR[name],
        JOB_RUN_ID,
        None,
    )


async def test_the_bound_request_id_is_recorded_for_users_and_anonymous_callers_not_subsystems(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    token = request_id_var.set(REQUEST_ID)
    try:
        await record(pure_recorder, UserActor(USER_ID))
        await record(pure_recorder, AnonymousActor(), action=LOGIN, category="authentication")
        await record(pure_recorder, SubsystemActor("notification-sender", None, JOB_RUN_ID))
    finally:
        request_id_var.reset(token)

    assert [row["request_id"] for row in recording.writes] == [REQUEST_ID, REQUEST_ID, None]


def test_an_unregistered_subsystem_name_raises_before_anything_is_written() -> None:
    with pytest.raises(UnknownSubsystem):
        SubsystemActor(UNREGISTERED, RULE_ID)


@pytest.mark.parametrize(("name", "rule_id"), WRONG_RULE_SHAPE)
def test_a_subsystem_takes_a_rule_exactly_when_the_registry_says_it_does(
    name: str, rule_id: int | None
) -> None:
    with pytest.raises(ValueError, match="rule_id"):
        SubsystemActor(name, rule_id)


@pytest.mark.parametrize("job_run_id", [0, -1], ids=["zero", "unrecorded-run"])
async def test_a_subsystem_event_naming_a_run_with_no_row_is_refused_before_anything_is_written(
    pure_recorder: ActivityRecorder, recording: RecordingSession, job_run_id: int
) -> None:
    actor = SubsystemActor(TAKES_NO_RULE[0], None, job_run_id)

    with pytest.raises(ValueError, match="no job_run row"):
        await record(pure_recorder, actor)

    assert recording.writes == []


@pytest.mark.parametrize("action", sorted(ANONYMOUS_ACTIONS))
async def test_an_anonymous_actor_may_record_each_listed_authentication_action(
    pure_recorder: ActivityRecorder, recording: RecordingSession, action: str
) -> None:
    await record(pure_recorder, AnonymousActor(), action=action, category="authentication")

    [row] = recording.writes
    assert (row["actor_type"], row["actor_user_id"], row["actor_subsystem"]) == (
        "anonymous",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("action", "category"),
    [
        pytest.param(LOGIN, "user_action", id="listed-action-in-another-category"),
        pytest.param(LOGIN, "automation", id="listed-action-as-automation"),
        pytest.param("user_deleted", "authentication", id="unlisted-action"),
    ],
)
async def test_an_anonymous_actor_outside_authentication_or_the_listed_actions_is_refused(
    pure_recorder: ActivityRecorder, recording: RecordingSession, action: str, category: str
) -> None:
    with pytest.raises(ValueError, match="cannot"):
        await record(pure_recorder, AnonymousActor(), action=action, category=category)

    assert recording.writes == []


async def test_a_subsystem_name_forced_past_the_constructor_is_refused_at_insert(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    actor = SubsystemActor("container-updater", RULE_ID)
    object.__setattr__(actor, "name", UNREGISTERED)

    with pytest.raises(UnknownSubsystem):
        await record(pure_recorder, actor)

    assert recording.writes == []


async def test_a_rule_removed_past_the_constructor_is_refused_at_insert(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    actor = SubsystemActor("container-updater", RULE_ID)
    object.__setattr__(actor, "rule_id", None)

    with pytest.raises(ValueError, match="rule_id"):
        await record(pure_recorder, actor)

    assert recording.writes == []


@dataclass(frozen=True, slots=True)
class _ForgedUser(UserActor):
    pass


@dataclass(frozen=True, slots=True)
class _ForgedSubsystem(SubsystemActor):
    pass


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param(_ForgedUser(USER_ID), id="user-subclass"),
        pytest.param(_ForgedSubsystem("container-updater", RULE_ID), id="subsystem-subclass"),
    ],
)
async def test_a_subclass_of_an_actor_is_refused_at_insert(
    pure_recorder: ActivityRecorder, recording: RecordingSession, actor: object
) -> None:
    with pytest.raises(TypeError, match="not an actor"):
        await record(pure_recorder, actor)

    assert recording.writes == []


def test_audited_refuses_a_method_with_no_actor_parameter_when_it_is_defined() -> None:
    async def change(self: object, key: str) -> None:
        return None

    with pytest.raises(TypeError, match="actor parameter"):
        audited(action=ACTION, target_type=TARGET)(change)  # type: ignore[type-var]


def test_the_open_audit_state_is_unavailable_outside_a_scope() -> None:
    with pytest.raises(RuntimeError, match="no audit scope"):
        current_audit()


async def test_an_anonymous_actor_cannot_own_an_audit_scope(
    pure_recorder: ActivityRecorder,
) -> None:
    with pytest.raises(ValueError, match="cannot own a mutation"):
        async with pure_recorder.audit(AnonymousActor(), action=LOGIN, target_type=TARGET):  # type: ignore[arg-type]
            pass
