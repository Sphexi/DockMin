"""replaced_image: the images a rule's updates replaced, waiting for their grace period."""

import dataclasses
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.db.engine import Database, Session
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.errors import MissingReferenceError
from app.db.repositories.replaced_image import ReplacedImage, ReplacedImageRepository
from tests.auth_support import FakeClock
from tests.support import present

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
STEP = timedelta(hours=1)
FIRST_IMAGE = "a" * 64
SECOND_IMAGE = "b" * 64
THIRD_IMAGE = "c" * 64
CONTAINER_ID = "1" * 64
LIMIT = 20
NO_SUCH_ID = 9999


@dataclass(slots=True)
class Records:
    session: Session
    clock: FakeClock
    environment: int
    user: int
    rule: AutomationRule
    repo: ReplacedImageRepository

    async def another_rule(self, name: str) -> AutomationRule:
        return await AutomationRuleRepository(self.session, self.clock).insert(
            name=name,
            subsystem="container-updater",
            environment_id=self.environment,
            target_selector="{}",
            parameters="{}",
            is_enabled=True,
            created_by_user_id=self.user,
        )

    async def add(self, image: str, rule: AutomationRule | None = None) -> None:
        await self.repo.add_replaced(
            rule or self.rule, self.environment, image, "container", CONTAINER_ID
        )

    async def due(
        self, *, after: timedelta = GRACE, rule: AutomationRule | None = None
    ) -> list[ReplacedImage]:
        return await self.repo.due(
            rule or self.rule, self.environment, self.clock() + after, GRACE, LIMIT
        )

    async def count(self) -> int:
        row = await self.session.fetch_one("SELECT COUNT(*) AS found FROM replaced_image")
        return int(present(row)["found"])


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Session]:
    async with database.session() as opened:
        yield opened


@pytest.fixture
async def records(session: Session) -> Records:
    clock = FakeClock(START)
    user = await session.execute(
        "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
        "VALUES ('owner', 'x', 'administrator', 1, ?)",
        (format_timestamp(START),),
    )
    environment = await session.execute(
        "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
        "VALUES ('prod', 'unix_socket', '/run/docker.sock', ?, ?)",
        (format_timestamp(START), format_timestamp(START)),
    )
    user_id, environment_id = present(user.last_row_id), present(environment.last_row_id)
    rule = await AutomationRuleRepository(session, clock).insert(
        name="keep current",
        subsystem="container-updater",
        environment_id=environment_id,
        target_selector="{}",
        parameters="{}",
        is_enabled=True,
        created_by_user_id=user_id,
    )
    return Records(
        session, clock, environment_id, user_id, rule, ReplacedImageRepository(session, clock)
    )


async def test_a_record_is_due_once_the_grace_period_has_passed(records: Records) -> None:
    await records.add(FIRST_IMAGE)

    waiting = await records.due(after=GRACE - timedelta(seconds=1))
    due = await records.due()

    assert waiting == []
    assert due == [
        ReplacedImage(
            rule_id=records.rule.id,
            rule_created_at=records.rule.created_at,
            environment_id=records.environment,
            image_id=FIRST_IMAGE,
            replaced_at=format_timestamp(START),
            target_type="container",
            target_id=CONTAINER_ID,
        )
    ]


async def test_due_records_come_oldest_first_and_no_more_than_the_limit(records: Records) -> None:
    for image in (SECOND_IMAGE, FIRST_IMAGE, THIRD_IMAGE):
        await records.add(image)
        records.clock.advance(STEP)

    everything = await records.due(after=GRACE + 3 * STEP)
    limited = await records.repo.due(
        records.rule, records.environment, records.clock() + GRACE, GRACE, 2
    )

    assert [found.image_id for found in everything] == [SECOND_IMAGE, FIRST_IMAGE, THIRD_IMAGE]
    assert [found.image_id for found in limited] == [SECOND_IMAGE, FIRST_IMAGE]


async def test_recording_an_image_again_restarts_its_wait_and_keeps_one_record(
    records: Records,
) -> None:
    await records.add(FIRST_IMAGE)
    records.clock.advance(STEP)
    await records.repo.add_replaced(records.rule, records.environment, FIRST_IMAGE, "stack", "shop")

    [only] = await records.due(after=GRACE + STEP)

    assert await records.count() == 1
    assert only.replaced_at == format_timestamp(START + STEP)
    assert (only.target_type, only.target_id) == ("stack", "shop")
    assert await records.due(after=GRACE - timedelta(seconds=1)) == []


async def test_a_record_of_one_rule_is_neither_due_for_nor_deleted_by_another(
    records: Records,
) -> None:
    other = await records.another_rule("other")
    await records.add(FIRST_IMAGE)

    assert await records.due(rule=other) == []
    assert await records.repo.delete(other, records.environment, FIRST_IMAGE) is False
    assert await records.count() == 1


async def test_delete_removes_one_record_and_says_whether_there_was_one(records: Records) -> None:
    await records.add(FIRST_IMAGE)
    await records.add(SECOND_IMAGE)

    removed = await records.repo.delete(records.rule, records.environment, FIRST_IMAGE)
    again = await records.repo.delete(records.rule, records.environment, FIRST_IMAGE)

    assert (removed, again) == (True, False)
    assert [found.image_id for found in await records.due()] == [SECOND_IMAGE]


async def test_delete_for_rule_removes_every_record_of_that_rule_id_and_no_others(
    records: Records,
) -> None:
    other = await records.another_rule("other")
    await records.add(FIRST_IMAGE)
    await records.add(SECOND_IMAGE)
    await records.add(THIRD_IMAGE, other)

    removed = await records.repo.delete_for_rule(records.rule.id)

    assert removed == 2
    assert [found.image_id for found in await records.due(rule=other)] == [THIRD_IMAGE]


async def test_records_written_under_another_creation_of_the_rule_id_are_ignored_then_dropped(
    records: Records,
) -> None:
    await records.add(FIRST_IMAGE)
    recreated = dataclasses.replace(records.rule, created_at="2027-01-01T00:00:00Z")

    due = await records.due(rule=recreated)
    waiting = await records.repo.waiting(recreated)
    deleted = await records.repo.delete(recreated, records.environment, FIRST_IMAGE)
    dropped = await records.repo.drop_stale(recreated)

    assert (due, waiting.count, deleted) == ([], 0, False)
    assert dropped == 1
    assert await records.count() == 0


async def test_recording_under_a_new_creation_of_the_rule_id_replaces_the_old_record(
    records: Records,
) -> None:
    await records.add(FIRST_IMAGE)
    recreated = dataclasses.replace(records.rule, created_at="2027-01-01T00:00:00Z")

    await records.add(FIRST_IMAGE, recreated)

    [found] = await records.due(after=GRACE, rule=recreated)
    assert found.rule_created_at == recreated.created_at
    assert await records.count() == 1
    assert await records.due() == []


async def test_waiting_counts_the_rules_records_and_reports_the_oldest_write_time(
    records: Records,
) -> None:
    empty = await records.repo.waiting(records.rule)
    await records.add(SECOND_IMAGE)
    records.clock.advance(STEP)
    await records.add(FIRST_IMAGE)

    waiting = await records.repo.waiting(records.rule)

    assert (empty.count, empty.oldest_replaced_at) == (0, None)
    assert (waiting.count, waiting.oldest_replaced_at) == (2, format_timestamp(START))


async def test_a_record_for_a_rule_or_environment_that_does_not_exist_is_refused(
    records: Records,
) -> None:
    ghost_rule = dataclasses.replace(records.rule, id=NO_SUCH_ID)

    with pytest.raises(MissingReferenceError):
        await records.add(FIRST_IMAGE, ghost_rule)
    with pytest.raises(MissingReferenceError):
        await records.repo.add_replaced(
            records.rule, NO_SUCH_ID, FIRST_IMAGE, "container", CONTAINER_ID
        )


@pytest.mark.parametrize(
    "image_id", ["a" * 63, "A" * 64, "sha256:" + "a" * 57], ids=["short", "upper-case", "prefixed"]
)
async def test_an_image_id_that_is_not_64_lower_case_hex_digits_is_refused_by_the_table(
    records: Records, image_id: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await records.add(image_id)


async def test_deleting_the_rule_or_the_environment_deletes_its_records(records: Records) -> None:
    other = await records.another_rule("other")
    await records.add(FIRST_IMAGE)
    await records.add(SECOND_IMAGE, other)

    await AutomationRuleRepository(records.session, records.clock).delete(records.rule.id)
    after_rule = await records.count()
    await records.session.execute("DELETE FROM environment WHERE id = ?", (records.environment,))

    assert after_rule == 1
    assert await records.count() == 0
