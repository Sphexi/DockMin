"""The read side: each filter, ordering, cursors, and who may read."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.db.engine import Database, Session, connect
from app.db.repositories.activity_event import ActivityEventRepository, ActivityFilter
from app.db.repositories.pagination import InvalidCursorError
from app.services.activity_log import ActivityLogService, NoRetention, RetentionResult
from tests.access_support import ADMIN, STANDARD, seed_world
from tests.activity.support import RecordingSession
from tests.auth_support import provision
from tests.db.helpers import walk_pages

INSERT_EVENT = (
    "INSERT INTO activity_event (action, category, actor_type, actor_user_id, actor_subsystem, "
    "rule_id, environment_id, target_type, target_id, result, occurred_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, updated_at) "
    "VALUES (?, ?, '{}', '2030-01-01T00:00:00Z', '2030-01-01T00:00:00Z')"
)
PAGE = 50
SMALL_PAGE = 7
BURST_SIZE = 25
BURST_STAMP = "2030-01-01T11:00:00Z"
EARLIER_STAMP = "2030-01-01T10:00:00Z"
LATER_STAMP = "2030-01-01T12:00:00Z"
UPDATER = "container-updater"
BACKUP = "backup-syncer"
REFRESHER = "environment-snapshot-refresher"


def at(second: int) -> str:
    return f"2030-01-01T10:00:{second:02d}Z"


def moment(second: int) -> datetime:
    return datetime(2030, 1, 1, 10, 0, second, tzinfo=UTC)


# label, category, actor_type, user, subsystem, rule, environment, target type, target id, result
EVENTS = [
    ("e01", "authentication", "anonymous", None, None, None, None, "user", "1", "failure", at(1)),
    ("e02", "authentication", "user", 1, None, None, None, "user", "1", "success", at(2)),
    ("e03", "user_action", "user", 1, None, None, 1, "container", "c1", "success", at(3)),
    ("e04", "user_action", "user", 2, None, None, 1, "container", "c2", "failure", at(4)),
    ("e05", "user_action", "user", 2, None, None, 2, "container", "c1", "success", at(5)),
    ("e06", "automation", "subsystem", None, UPDATER, 1, 1, "container", "c1", "success", at(6)),
    ("e07", "automation", "subsystem", None, UPDATER, 1, 2, "container", "c2", "failure", at(7)),
    ("e08", "automation", "subsystem", None, BACKUP, 2, None, "backup", "b1", "success", at(8)),
    (
        "e09",
        "automation",
        "subsystem",
        None,
        REFRESHER,
        None,
        1,
        "environment",
        "1",
        "success",
        at(9),
    ),
    ("e10", "user_action", "user", 1, None, None, None, "team", "t1", "success", at(10)),
]
EVERY_LABEL = {event[0] for event in EVENTS}

FILTER_CASES = [
    pytest.param(ActivityFilter(), EVERY_LABEL, id="no-filter"),
    pytest.param(ActivityFilter(category="authentication"), {"e01", "e02"}, id="category"),
    pytest.param(ActivityFilter(actor_type="anonymous"), {"e01"}, id="actor-type-anonymous"),
    pytest.param(
        ActivityFilter(actor_type="subsystem"), {"e06", "e07", "e08", "e09"}, id="actor-type"
    ),
    pytest.param(ActivityFilter(actor_user_id=2), {"e04", "e05"}, id="actor-user"),
    pytest.param(ActivityFilter(actor_subsystem=UPDATER), {"e06", "e07"}, id="subsystem"),
    pytest.param(ActivityFilter(rule_id=1), {"e06", "e07"}, id="rule"),
    pytest.param(ActivityFilter(environment_id=1), {"e03", "e04", "e06", "e09"}, id="environment"),
    pytest.param(
        ActivityFilter(target_type="container", target_id="c1", environment_id=1),
        {"e03", "e06"},
        id="target-in-an-environment",
    ),
    pytest.param(
        ActivityFilter(target_type="container", target_id="c1", environment_id=2),
        {"e05"},
        id="same-target-in-another-environment",
    ),
    pytest.param(
        ActivityFilter(target_type="user", target_id="1"),
        {"e01", "e02"},
        id="target-with-no-environment",
    ),
    pytest.param(
        ActivityFilter(target_type="container", target_id="c1"),
        set(),
        id="target-that-only-exists-inside-an-environment",
    ),
    pytest.param(ActivityFilter(result="failure"), {"e01", "e04", "e07"}, id="result"),
    pytest.param(
        ActivityFilter(occurred_from=moment(5), occurred_to=moment(8)),
        {"e05", "e06", "e07"},
        id="time-range-includes-from-and-excludes-to",
    ),
    pytest.param(
        ActivityFilter(category="user_action", result="success"),
        {"e03", "e05", "e10"},
        id="two-fields-together",
    ),
]
FILTER_ERRORS = [
    pytest.param({"target_type": "container"}, "set together", id="target-type-alone"),
    pytest.param({"target_id": "c1"}, "set together", id="target-id-alone"),
    pytest.param({"occurred_from": datetime(2030, 1, 1)}, "timezone-aware", id="naive-from"),
    pytest.param({"occurred_to": datetime(2030, 1, 1)}, "timezone-aware", id="naive-to"),
]
BAD_CURSORS = ["not base64 !!", "WzFd"]


@pytest.fixture(scope="module")
def seeded(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated database holding a fixed set of events; tests only read it."""
    path = tmp_path_factory.mktemp("activity-read") / "dockmin.db"
    provision(migrated_template, path, with_sessions=False)
    asyncio.run(seed_world(path))
    connection = connect(path)
    try:
        connection.execute(INSERT_RULE, ("updates", UPDATER))
        connection.execute(INSERT_RULE, ("backups", BACKUP))
        connection.executemany(INSERT_EVENT, EVENTS)
    finally:
        connection.close()
    return path


@pytest.fixture
async def seeded_db(seeded: Path) -> AsyncIterator[Session]:
    async with Database(seeded).session() as opened:
        yield opened


@pytest.mark.parametrize(("criteria", "expected"), FILTER_CASES)
async def test_each_filter_returns_exactly_the_matching_events(
    seeded_db: Session, criteria: ActivityFilter, expected: set[str]
) -> None:
    page = await ActivityEventRepository(seeded_db).page(criteria, limit=PAGE)

    assert {event.action for event in page.items} == expected
    assert page.next_cursor is None


async def test_events_come_back_newest_first(seeded_db: Session) -> None:
    page = await ActivityEventRepository(seeded_db).page(ActivityFilter(), limit=PAGE)

    assert [event.action for event in page.items] == sorted(EVERY_LABEL, reverse=True)


@pytest.mark.parametrize(("fields", "message"), FILTER_ERRORS)
def test_a_filter_with_a_half_target_or_a_naive_time_is_refused(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ActivityFilter(**fields)  # type: ignore[arg-type]


INSERT_BURST = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 31) "
    "INSERT INTO activity_event (category, actor_type, target_type, action, result, occurred_at) "
    "SELECT 'authentication', 'anonymous', 'probe', 'burst' || i, 'success', "
    "CASE WHEN i <= 3 THEN ? WHEN i <= 28 THEN ? ELSE ? END FROM n"
)
BURST_ORDER = [*range(31, 28, -1), *range(28, 3, -1), *range(3, 0, -1)]


async def test_paging_through_events_that_share_a_timestamp_returns_each_once_newest_first(
    db: Session,
) -> None:
    await db.execute(INSERT_BURST, (EARLIER_STAMP, BURST_STAMP, LATER_STAMP))
    repository = ActivityEventRepository(db)

    walk = await walk_pages(
        lambda *, limit, cursor: repository.page(ActivityFilter(), limit=limit, cursor=cursor),
        limit=SMALL_PAGE,
        key=lambda event: event.id,
    )

    assert walk.keys == BURST_ORDER
    assert walk.page_sizes == [SMALL_PAGE, SMALL_PAGE, SMALL_PAGE, SMALL_PAGE, 3]


@pytest.mark.parametrize("cursor", BAD_CURSORS)
async def test_a_tampered_cursor_is_refused_before_any_query_runs(
    recording: RecordingSession, cursor: str
) -> None:
    repository = ActivityEventRepository(recording)  # type: ignore[arg-type]

    with pytest.raises(InvalidCursorError):
        await repository.page(ActivityFilter(), limit=PAGE, cursor=cursor)

    assert recording.queries == []


async def test_an_administrator_reads_the_log_through_the_service(seeded_db: Session) -> None:
    service = ActivityLogService(db=seeded_db)

    page = await service.page(ADMIN, ActivityFilter(actor_subsystem=UPDATER), limit=PAGE)

    assert {view.event.action for view in page.items} == {"e06", "e07"}


async def test_a_standard_user_is_refused_before_any_query_runs(
    recording: RecordingSession,
) -> None:
    service = ActivityLogService(db=recording)  # type: ignore[arg-type]

    with pytest.raises(PermissionDenied):
        await service.page(STANDARD, ActivityFilter(), limit=PAGE)

    assert recording.queries == []


async def test_a_subsystem_filter_naming_no_registered_subsystem_is_refused(
    recording: RecordingSession,
) -> None:
    service = ActivityLogService(db=recording)  # type: ignore[arg-type]

    with pytest.raises(ValidationFailed) as refused:
        await service.page(ADMIN, ActivityFilter(actor_subsystem="container-restarter"), limit=PAGE)

    assert set(refused.value.fields) == {"actor_subsystem"}
    assert recording.queries == []


async def test_the_default_retention_policy_removes_nothing() -> None:
    result = await NoRetention().apply(moment(0))

    assert result == RetentionResult(removed=0)
