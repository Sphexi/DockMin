"""Every row a rule's run writes names the rule, whichever recorder writes it."""

import json
from dataclasses import dataclass

import pytest

from app.db.engine import Session
from app.db.repositories.activity_event import MAX_DETAIL_KEYS
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.user import UserRepository
from app.services.acting import AutomationAuthority
from app.services.activity import ActivityRecorder, SubsystemActor, UserActor, audited
from tests.activity.support import RecordingSession, read_rows
from tests.db.helpers import PASSWORD_HASH

RESTARTER = "stack-restarter"
REFRESHER = "environment-snapshot-refresher"
RULE_ID = 11
RUN_ID = 13
RULE_NAME = "Nightly restart"
ACTION = "probe"
TARGET = "probe_target"
OWNER_NAME = "rule-owner"


async def record(recorder: ActivityRecorder, actor: object, **detail: object) -> None:
    await recorder.record_event(
        actor,  # type: ignore[arg-type]
        action=ACTION,
        target_type=TARGET,
        result="success",
        detail=detail or None,  # type: ignore[arg-type]
    )


def stored(recording: RecordingSession) -> dict[str, object] | None:
    [written] = recording.writes
    return None if written["detail"] is None else json.loads(written["detail"])


async def test_a_plain_recorder_stamps_the_rule_id_and_name_into_a_subsystems_row(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    await record(pure_recorder, SubsystemActor(RESTARTER, RULE_ID, RUN_ID, RULE_NAME), name="web")

    assert stored(recording) == {"name": "web", "rule_id": RULE_ID, "rule_name": RULE_NAME}


async def test_a_value_the_caller_supplied_is_not_overwritten(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    actor = SubsystemActor(RESTARTER, RULE_ID, RUN_ID, RULE_NAME)

    await record(pure_recorder, actor, rule_name="chosen")

    assert stored(recording) == {"rule_name": "chosen", "rule_id": RULE_ID}


async def test_an_actor_without_a_rule_name_stamps_the_id_alone(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    await record(pure_recorder, SubsystemActor(RESTARTER, RULE_ID, RUN_ID))

    assert stored(recording) == {"rule_id": RULE_ID}


@pytest.mark.parametrize("actor", [SubsystemActor(REFRESHER, None, RUN_ID), UserActor(7)])
async def test_an_actor_with_no_rule_leaves_the_detail_alone(
    pure_recorder: ActivityRecorder, recording: RecordingSession, actor: object
) -> None:
    await record(pure_recorder, actor)

    assert stored(recording) is None


async def test_a_detail_already_at_the_key_limit_is_not_stamped_past_it(
    pure_recorder: ActivityRecorder, recording: RecordingSession
) -> None:
    full = {f"key{n}": n for n in range(MAX_DETAIL_KEYS)}

    await record(pure_recorder, SubsystemActor(RESTARTER, RULE_ID, RUN_ID, RULE_NAME), **full)

    assert stored(recording) == full


def test_the_rule_name_does_not_make_two_actors_of_one_run_different() -> None:
    named = SubsystemActor(RESTARTER, RULE_ID, RUN_ID, RULE_NAME)

    assert named == SubsystemActor(RESTARTER, RULE_ID, RUN_ID, "Renamed")
    assert named != SubsystemActor(RESTARTER, RULE_ID + 1, RUN_ID, RULE_NAME)


@dataclass
class Acting:
    """Anything that acts as a subsystem exposes its actor and is accepted as one."""

    actor: SubsystemActor

    @property
    def subsystem_actor(self) -> SubsystemActor:
        return self.actor


class Service:
    def __init__(self, activity: ActivityRecorder, db: Session) -> None:
        self._activity = activity
        self._db = db

    @audited(action="probe_changed", target_type="thing", target_id=lambda a: "1")
    async def change(self, actor: object) -> None:
        async with self._db.transaction():
            await self._db.execute("SELECT 1")


async def authority_of(actor: SubsystemActor) -> AutomationAuthority:
    async def authorise(_key: object) -> None:
        return None

    return AutomationAuthority(
        actor=actor,
        authorise=authorise,
        hold=lambda _key, _action: None,  # type: ignore[arg-type,return-value]
        lock_wait=1.0,
        authorise_read=authorise,
        owner_is_administrator=True,
        authorise_credential_set=authorise,
    )


@pytest.mark.parametrize("kind", ["acting", "authority"])
async def test_an_audited_method_records_whatever_acts_as_a_subsystem_under_that_subsystem(
    recorder: ActivityRecorder, db: Session, kind: str
) -> None:
    actor = SubsystemActor(RESTARTER, await rule_in(db), None, RULE_NAME)
    acting = Acting(actor) if kind == "acting" else await authority_of(actor)

    await Service(recorder, db).change(acting)

    [row] = await read_rows(db)
    assert (row["actor_type"], row["actor_subsystem"], row["category"]) == (
        "subsystem",
        RESTARTER,
        "automation",
    )
    assert row["actor_user_id"] is None
    assert row["detail"] == {"rule_id": actor.rule_id, "rule_name": RULE_NAME}


async def test_an_audited_method_refuses_an_actor_of_no_known_kind(
    recorder: ActivityRecorder, db: Session
) -> None:
    with pytest.raises(TypeError):
        await Service(recorder, db).change(object())


async def rule_in(db: Session) -> int:
    owner = await UserRepository(db).create(OWNER_NAME, PASSWORD_HASH, "administrator")
    rule = await AutomationRuleRepository(db).insert(
        name=RULE_NAME,
        subsystem=RESTARTER,
        environment_id=None,
        target_selector="",
        parameters="{}",
        is_enabled=False,
        created_by_user_id=owner.id,
    )
    return rule.id
