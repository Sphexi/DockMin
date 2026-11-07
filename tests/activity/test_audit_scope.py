"""The audit scope: what each way of leaving it records, and what it must never lose."""

import asyncio
import sqlite3
from typing import Any

import pytest

from app.auth.errors import NotFound, PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database, Session
from app.db.repositories.activity_event import MAX_DETAIL_KEYS, ActivityEventRepository
from app.db.repositories.user import UserRepository
from app.services.activity import (
    ActivityRecorder,
    MultipleCommits,
    SubsystemActor,
    UserActor,
    audited,
    current_audit,
)
from app.subsystems.base import RuleEventRecorder
from tests.activity.support import read_rows
from tests.auth_support import FakeClock
from tests.db.helpers import PASSWORD_HASH

ACTION = "probe_changed"
TARGET = "probe"
TARGET_ID = "42"
ADDRESS = "198.51.100.7"
ABSENT_ID = 999
OTHER_ABSENT_ID = 998
RULE_NAME = "Nightly restart"
STAMP = "2026-01-02T03:04:05Z"

INSERT_PROBE = "INSERT INTO probe (value) VALUES (?)"
INSERT_ORPHAN = "INSERT INTO orphan (user_id) VALUES (?)"
FULL_DETAIL = {f"key{n}": n for n in range(MAX_DETAIL_KEYS)}
OVERFULL_DETAIL = FULL_DETAIL | {"one_too_many": 0}

PRODUCT_ERROR = ValidationFailed({"name": "is taken"})
FAILURE_CASES = [
    pytest.param(
        PRODUCT_ERROR,
        None,
        {"address": ADDRESS, "reason": "ValidationFailed", "message": str(PRODUCT_ERROR)},
        id="product-error-keeps-its-message",
    ),
    pytest.param(
        RuntimeError("secret text"),
        None,
        {"address": ADDRESS, "reason": "RuntimeError"},
        id="foreign-error-stores-only-its-class",
    ),
    pytest.param(
        RuntimeError("secret text"),
        "bad_current_password",
        {"address": ADDRESS, "reason": "bad_current_password"},
        id="an-explicit-reason-wins",
    ),
]
UNLINKED_CASES = [
    pytest.param(
        SubsystemActor("environment-snapshot-refresher", None),
        ABSENT_ID,
        {"environment_id": str(ABSENT_ID), "reason": "RuntimeError"},
        (None, None, None),
        id="environment",
    ),
    pytest.param(
        SubsystemActor("container-updater", ABSENT_ID, OTHER_ABSENT_ID),
        None,
        {
            "rule_id": str(ABSENT_ID),
            "job_run_id": str(OTHER_ABSENT_ID),
            "reason": "RuntimeError",
        },
        (None, None, None),
        id="rule-and-job-run",
    ),
]


@pytest.fixture
async def actor(db: Session) -> UserActor:
    created = await UserRepository(db).create("scope-user", PASSWORD_HASH, "standard")
    return UserActor(created.id)


@pytest.fixture
async def probe(db: Session) -> None:
    await db.execute("CREATE TABLE probe (value TEXT NOT NULL)")
    await db.execute("CREATE TABLE orphan (user_id INTEGER REFERENCES user (id))")


async def probe_values(db: Session) -> list[str]:
    rows = await db.fetch_limited("SELECT value FROM probe ORDER BY rowid", (), limit=100)
    return [row["value"] for row in rows]


async def write_probe(db: Session, value: str) -> None:
    async with db.transaction():
        await db.execute(INSERT_PROBE, (value,))


class _Service:
    def __init__(self, activity: ActivityRecorder, db: Session) -> None:
        self._activity = activity
        self._db = db

    @audited(
        action=lambda a: a["verb"],
        target_type="thing",
        target_id=lambda a: str(a["thing_id"]),
        environment_id=lambda a: a["environment"],
    )
    async def change(
        self,
        actor: object,
        thing_id: int,
        *,
        verb: str,
        environment: int | None,
        remote_address: str,
    ) -> None:
        async with self._db.transaction():
            await self._db.execute(INSERT_PROBE, (verb,))
            current_audit().detail["extra"] = thing_id


async def test_a_committed_block_leaves_its_mutation_and_one_success_row_together(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None
) -> None:
    async with recorder.audit(actor, action=ACTION, target_type=TARGET) as state, db.transaction():
        await db.execute(INSERT_PROBE, ("kept",))
        state.target_id = TARGET_ID
        state.detail["role"] = "standard"

    [row] = await read_rows(db)
    assert await probe_values(db) == ["kept"]
    assert (row["result"], row["action"], row["target_id"], row["actor_user_id"]) == (
        "success",
        ACTION,
        TARGET_ID,
        actor.user_id,
    )
    assert row["detail"] == {"role": "standard"}


async def test_a_block_that_opens_no_transaction_still_leaves_a_success_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with recorder.audit(actor, action=ACTION, target_type=TARGET, target_id=TARGET_ID):
        pass

    [row] = await read_rows(db)
    assert (row["result"], row["target_id"]) == ("success", TARGET_ID)


async def test_a_block_whose_transaction_rolls_back_leaves_no_mutation_and_no_success_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None
) -> None:
    with pytest.raises(RuntimeError):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET), db.transaction():
            await db.execute(INSERT_PROBE, ("lost",))
            raise RuntimeError("abort")

    rows = await read_rows(db)
    assert await probe_values(db) == []
    assert [row["result"] for row in rows] == ["failure"]


async def test_a_success_row_that_cannot_be_written_rolls_the_mutation_back(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None
) -> None:
    with pytest.raises(ValueError, match="detail holds at most"):
        async with (
            recorder.audit(actor, action=ACTION, target_type=TARGET) as state,
            db.transaction(),
        ):
            await db.execute(INSERT_PROBE, ("lost",))
            state.detail.update(OVERFULL_DETAIL)

    rows = await read_rows(db)
    assert await probe_values(db) == []
    assert [(row["result"], row["detail"]) for row in rows] == [
        ("failure", {"reason": "ValueError"})
    ]


@pytest.mark.parametrize(("error", "reason", "expected_detail"), FAILURE_CASES)
async def test_a_raising_block_leaves_one_failure_row_naming_the_reason(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    error: Exception,
    reason: str | None,
    expected_detail: dict[str, Any],
) -> None:
    with pytest.raises(type(error)):
        async with recorder.audit(
            actor, action=ACTION, target_type=TARGET, target_id=TARGET_ID
        ) as state:
            state.detail["address"] = ADDRESS
            state.reason = reason
            raise error

    [row] = await read_rows(db)
    assert (row["result"], row["target_id"], row["actor_user_id"]) == (
        "failure",
        TARGET_ID,
        actor.user_id,
    )
    assert row["detail"] == expected_detail


async def test_a_body_whose_detail_fills_every_key_still_leaves_its_failure_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    with pytest.raises(RuntimeError):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET) as state:
            state.detail.update(FULL_DETAIL)
            raise RuntimeError("abort")

    [row] = await read_rows(db)
    assert (row["result"], row["detail"]) == ("failure", {"reason": "RuntimeError"})


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(PermissionDenied("denied"), id="permission-denied"),
        pytest.param(NotFound("missing"), id="not-found"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_a_refusal_or_a_cancellation_leaves_no_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor, error: BaseException
) -> None:
    with pytest.raises(type(error)):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET):
            raise error

    assert await read_rows(db) == []


async def test_a_block_that_reports_no_change_leaves_no_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with recorder.audit(actor, action=ACTION, target_type=TARGET) as state, db.transaction():
        state.unchanged()

    assert await read_rows(db) == []


async def test_a_failing_failure_write_re_raises_the_original_and_logs_only_the_class(
    recorder: ActivityRecorder,
    actor: UserActor,
    monkeypatch: pytest.MonkeyPatch,
    logged: Any,
    log_stream: Any,
) -> None:
    async def refuse(self: ActivityEventRepository, **columns: Any) -> None:
        raise sqlite3.DatabaseError("write refused: secret text")

    monkeypatch.setattr(ActivityEventRepository, "insert", refuse)

    with pytest.raises(ValidationFailed) as raised:
        async with recorder.audit(actor, action=ACTION, target_type=TARGET):
            raise PRODUCT_ERROR

    [record] = logged("activity_event_write_failed")
    assert raised.value is PRODUCT_ERROR
    assert (record["error"], record["result"], record["reason"]) == (
        "DatabaseError",
        "failure",
        "ValidationFailed",
    )
    assert "secret text" not in log_stream.getvalue()


async def test_a_success_row_that_cannot_be_written_without_a_transaction_is_logged_not_raised(
    recorder: ActivityRecorder,
    actor: UserActor,
    monkeypatch: pytest.MonkeyPatch,
    logged: Any,
) -> None:
    async def refuse(self: ActivityEventRepository, **columns: Any) -> None:
        raise sqlite3.DatabaseError("write refused")

    monkeypatch.setattr(ActivityEventRepository, "insert", refuse)

    async with recorder.audit(actor, action=ACTION, target_type=TARGET):
        pass

    [record] = logged("activity_event_write_failed")
    assert (record["result"], record["error"]) == ("success", "DatabaseError")


async def test_entering_a_scope_inside_a_transaction_raises(
    recorder: ActivityRecorder, db: Session, actor: UserActor
) -> None:
    async with db.transaction():
        with pytest.raises(RuntimeError, match="outside any transaction"):
            async with recorder.audit(actor, action=ACTION, target_type=TARGET):
                pass


async def test_a_scope_opened_through_another_recorder_on_the_same_session_raises(
    recorder: ActivityRecorder, db: Session, clock: FakeClock, actor: UserActor
) -> None:
    other = ActivityRecorder(db, clock)

    with pytest.raises(RuntimeError, match="do not nest"):
        async with (
            recorder.audit(actor, action=ACTION, target_type=TARGET),
            other.audit(actor, action=ACTION, target_type=TARGET),
        ):
            pass

    rows = await read_rows(db)
    assert [(row["result"], row["detail"]) for row in rows] == [
        ("failure", {"reason": "RuntimeError"})
    ]


async def test_a_second_commit_inside_a_scope_raises_and_is_rolled_back(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None
) -> None:
    with pytest.raises(MultipleCommits):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET):
            await write_probe(db, "first")
            await write_probe(db, "second")

    rows = await read_rows(db)
    assert await probe_values(db) == ["first"]
    assert [(row["result"], row["detail"]) for row in rows] == [
        ("success", None),
        ("failure", {"reason": "MultipleCommits"}),
    ]


async def test_a_commit_that_fails_after_the_hook_leaves_a_commit_failed_row_and_no_mutation(
    recorder: ActivityRecorder, db: Session, database: Database, actor: UserActor, probe: None
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET), db.transaction():
            await db.execute("PRAGMA defer_foreign_keys = ON")
            await db.execute(INSERT_PROBE, ("lost",))
            await db.execute(INSERT_ORPHAN, (ABSENT_ID,))

    async with database.session() as other:
        seen_elsewhere = await read_rows(other)
    assert not db.in_transaction
    assert await probe_values(db) == []
    assert [(row["result"], row["detail"]) for row in seen_elsewhere] == [
        ("failure", {"reason": "commit_failed"})
    ]


@pytest.mark.parametrize(
    ("subsystem", "environment_id", "expected_detail", "expected_links"), UNLINKED_CASES
)
async def test_a_failure_row_naming_something_that_does_not_exist_is_kept_with_the_ids_in_detail(
    recorder: ActivityRecorder,
    db: Session,
    subsystem: SubsystemActor,
    environment_id: int | None,
    expected_detail: dict[str, Any],
    expected_links: tuple[None, None, None],
) -> None:
    with pytest.raises(RuntimeError):
        async with recorder.audit(
            subsystem, action=ACTION, target_type=TARGET, environment_id=environment_id
        ):
            raise RuntimeError("abort")

    [row] = await read_rows(db)
    assert (row["environment_id"], row["rule_id"], row["job_run_id"]) == expected_links
    assert row["detail"] == expected_detail


async def test_a_block_whose_rule_and_run_are_gone_leaves_its_success_row_with_the_ids_in_detail(
    recorder: ActivityRecorder, db: Session, logged: Any
) -> None:
    gone = SubsystemActor("container-updater", ABSENT_ID, OTHER_ABSENT_ID, RULE_NAME)

    async with recorder.audit(gone, action=ACTION, target_type=TARGET) as state:
        pass

    [row] = await read_rows(db)
    assert (row["result"], row["rule_id"], row["job_run_id"]) == ("success", None, None)
    assert row["detail"] == {
        "rule_id": str(ABSENT_ID),
        "rule_name": RULE_NAME,
        "job_run_id": str(OTHER_ABSENT_ID),
    }
    assert state.written is True
    assert logged("activity_event_write_failed") == []


async def test_a_success_row_refused_for_another_integrity_error_is_logged_not_unlinked(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    monkeypatch: pytest.MonkeyPatch,
    logged: Any,
) -> None:
    async def refuse(self: ActivityEventRepository, **columns: Any) -> None:
        raise sqlite3.IntegrityError("UNIQUE constraint failed: activity_event.id")

    monkeypatch.setattr(ActivityEventRepository, "insert", refuse)

    async with recorder.audit(actor, action=ACTION, target_type=TARGET) as state:
        pass

    [record] = logged("activity_event_write_failed")
    assert (record["result"], record["error"]) == ("success", "IntegrityError")
    assert state.written is False
    assert await read_rows(db) == []


async def test_a_run_recorder_reports_an_unlinked_write_only_after_one_is_made(
    db: Session, clock: FakeClock
) -> None:
    run_recorder = RuleEventRecorder(db, clock)
    gone = SubsystemActor("container-updater", ABSENT_ID, OTHER_ABSENT_ID, RULE_NAME)
    before = run_recorder.wrote_unlinked

    async with run_recorder.audit(gone, action=ACTION, target_type=TARGET):
        pass

    assert (before, run_recorder.wrote_unlinked) == (False, True)


async def test_an_exception_raised_after_the_scopes_transaction_committed_adds_no_failure_row(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None
) -> None:
    with pytest.raises(RuntimeError):
        async with recorder.audit(actor, action=ACTION, target_type=TARGET):
            await write_probe(db, "kept")
            raise RuntimeError("after the commit")

    rows = await read_rows(db)
    assert await probe_values(db) == ["kept"]
    assert [row["result"] for row in rows] == ["success"]


async def test_a_decorated_method_records_its_resolved_names_its_address_and_what_it_filled_in(
    recorder: ActivityRecorder, db: Session, probe: None
) -> None:
    created = await UserRepository(db).create("caller", PASSWORD_HASH, "standard")
    environment = await db.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES ('scope-env', 'standalone', 'unix_socket', '/s', ?, ?)",
        (STAMP, STAMP),
    )
    caller = Principal(created.id, "caller", "standard")

    await _Service(recorder, db).change(
        caller,
        int(TARGET_ID),
        verb="restarted",
        environment=environment.last_row_id,
        remote_address=ADDRESS,
    )

    [row] = await read_rows(db)
    assert (row["action"], row["target_type"], row["target_id"], row["category"]) == (
        "restarted",
        "thing",
        TARGET_ID,
        "user_action",
    )
    assert (row["actor_user_id"], row["environment_id"]) == (created.id, environment.last_row_id)
    assert row["detail"] == {"address": ADDRESS, "extra": int(TARGET_ID)}


async def test_a_decorated_method_refuses_an_actor_that_is_not_a_user_or_subsystem(
    recorder: ActivityRecorder, db: Session
) -> None:
    with pytest.raises(TypeError, match="actor must be"):
        await _Service(recorder, db).change(
            "someone",
            1,
            verb="restarted",
            environment=None,
            remote_address=ADDRESS,
        )

    assert await read_rows(db) == []


NOTIFYING_ACTION = "environment_unreachable"
REFRESHER_ACTOR = SubsystemActor("environment-snapshot-refresher", None)


async def test_a_notifying_row_written_outside_the_blocks_own_transaction_is_written_once(
    recorder: ActivityRecorder, db: Session
) -> None:
    async with recorder.audit(REFRESHER_ACTOR, action=NOTIFYING_ACTION, target_type="environment"):
        pass

    assert [row["result"] for row in await read_rows(db)] == ["success"]


async def test_a_notifying_failure_row_is_not_followed_by_a_success_row(
    recorder: ActivityRecorder, db: Session
) -> None:
    with pytest.raises(RuntimeError):
        async with recorder.audit(
            REFRESHER_ACTOR, action=NOTIFYING_ACTION, target_type="environment"
        ):
            raise RuntimeError

    assert [row["result"] for row in await read_rows(db)] == ["failure"]
