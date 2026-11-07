"""Related rows: written only inside an audit scope's transaction, and committed with its row."""

import pytest

from app.db.engine import Database, Session
from app.db.repositories.user import UserRepository
from app.services.activity import ActivityRecorder, UserActor
from tests.activity.support import read_rows
from tests.auth_support import FakeClock
from tests.db.helpers import PASSWORD_HASH

ACTION = "image_pulled"
RELATED_ACTION = "image_bound"
RELATED_TARGET = "image_binding"
REFERENCE = "docker.io/library/busybox:latest"
DETAIL = {"credential_set_id": 3, "anonymous": False}


@pytest.fixture
async def actor(db: Session) -> UserActor:
    created = await UserRepository(db).create("related-user", PASSWORD_HASH, "standard")
    return UserActor(created.id)


async def related(recorder: ActivityRecorder, actor: UserActor) -> None:
    await recorder.record_related(
        actor,
        action=RELATED_ACTION,
        target_type=RELATED_TARGET,
        target_id=REFERENCE,
        environment_id=None,
        detail=DETAIL,
    )


async def test_a_related_row_outside_any_scope_is_refused(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with db.transaction():
        with pytest.raises(RuntimeError, match="audit scope"):
            await related(recorder, actor)

    assert await read_rows(db) == []


async def test_a_related_row_inside_a_scope_but_outside_a_transaction_is_refused(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with recorder.audit(actor, action=ACTION, target_type="image"):
        with pytest.raises(RuntimeError, match="transaction"):
            await related(recorder, actor)

    assert [row["action"] for row in await read_rows(db)] == [ACTION]


async def test_a_related_row_in_another_recorders_scope_is_refused(
    database: Database, recorder: ActivityRecorder, db: Session, actor: UserActor, clock: FakeClock
) -> None:
    async with database.session() as other:
        stranger = ActivityRecorder(other, clock)
        async with recorder.audit(actor, action=ACTION, target_type="image"), db.transaction():
            with pytest.raises(RuntimeError, match="audit scope"):
                await related(stranger, actor)


async def test_a_related_row_commits_with_the_scope_and_takes_its_category(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with (
        recorder.audit(actor, action=ACTION, target_type="image", environment_id=None),
        db.transaction(),
    ):
        await related(recorder, actor)

    scope_row, related_row = sorted(await read_rows(db), key=lambda row: row["action"] != ACTION)
    assert (related_row["action"], related_row["result"]) == (RELATED_ACTION, "success")
    assert (related_row["target_type"], related_row["target_id"]) == (RELATED_TARGET, REFERENCE)
    assert related_row["detail"] == DETAIL
    assert related_row["category"] == scope_row["category"]
    assert related_row["actor_user_id"] == actor.user_id


async def test_a_related_row_vanishes_when_the_scope_rolls_back(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    with pytest.raises(RuntimeError, match="abort"):
        async with recorder.audit(actor, action=ACTION, target_type="image"), db.transaction():
            await related(recorder, actor)
            raise RuntimeError("abort")

    assert [(row["action"], row["result"]) for row in await read_rows(db)] == [(ACTION, "failure")]
