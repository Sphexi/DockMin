"""The action, run and environment filters: results, the union with an environment's own events."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db.engine import Database, Session, connect
from app.db.repositories.activity_event import ActivityEventRepository, ActivityFilter
from tests.access_support import ENV_A, ENV_B, seed_world
from tests.auth_support import provision
from tests.db.helpers import walk_pages

INSERT_EVENT = (
    "INSERT INTO activity_event (action, category, actor_type, actor_user_id, actor_subsystem, "
    "rule_id, job_run_id, environment_id, target_type, target_id, result, occurred_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
UPDATER = "container-updater"
RULE = 1
FIRST_RUN = 1
SECOND_RUN = 2
ADMIN_USER = 1
STANDARD_USER = 2
BURST_SIZE = 30
BURST_PAGE = 7
BURST_STAMP = "2030-01-01T11:00:00Z"
RECREATED = "container_recreated"

USER, AUTOMATION = "user_action", "automation"


def event(
    action: str,
    *,
    category: str = USER,
    user: int | None = ADMIN_USER,
    subsystem: str | None = None,
    rule: int | None = None,
    run: int | None = None,
    environment: int | None = None,
    kind: str = "container",
    target: str = "c1",
    result: str = "success",
) -> tuple[object, ...]:
    actor = "subsystem" if subsystem else ("user" if user else "anonymous")
    return (action, category, actor, user, subsystem, rule, run, environment, kind, target, result)


# The row's id is its position and its time is that many seconds past the start.
EVENTS = [
    event("container_started", environment=ENV_A),
    event(
        "container_started", user=STANDARD_USER, environment=ENV_A, target="c2", result="failure"
    ),
    event("container_started", environment=ENV_B),
    event("environment_updated", kind="environment", target="1"),
    event("environment_updated", kind="environment", target="2"),
    event("environment_tested", environment=ENV_A, kind="environment", target="1"),
    event(
        RECREATED,
        category=AUTOMATION,
        user=None,
        subsystem=UPDATER,
        rule=RULE,
        run=FIRST_RUN,
        environment=ENV_A,
    ),
    event(
        RECREATED,
        category=AUTOMATION,
        user=None,
        subsystem=UPDATER,
        rule=RULE,
        run=FIRST_RUN,
        environment=ENV_A,
        target="c2",
        result="failure",
    ),
    event(
        RECREATED,
        category=AUTOMATION,
        user=None,
        subsystem=UPDATER,
        rule=RULE,
        run=SECOND_RUN,
        environment=ENV_B,
    ),
    event(
        "login_failed",
        category="authentication",
        user=None,
        kind="user",
        target="1",
        result="failure",
    ),
]


def stamp(second: int) -> str:
    return f"2030-01-01T10:00:{second:02d}Z"


def moment(second: int) -> datetime:
    return datetime(2030, 1, 1, 10, 0, second, tzinfo=UTC)


def seeded_rows() -> list[tuple[object, ...]]:
    return [(*row, stamp(number)) for number, row in enumerate(EVENTS, 1)]


FILTER_CASES = [
    pytest.param(ActivityFilter(action=RECREATED), {7, 8, 9}, id="action"),
    pytest.param(
        ActivityFilter(action=RECREATED, occurred_from=moment(8)), {8, 9}, id="action-time"
    ),
    pytest.param(ActivityFilter(job_run_id=FIRST_RUN), {7, 8}, id="job-run"),
    pytest.param(ActivityFilter(job_run_id=FIRST_RUN, result="failure"), {8}, id="job-run-result"),
    pytest.param(ActivityFilter(environment_id=ENV_A), {1, 2, 4, 6, 7, 8}, id="env"),
    pytest.param(ActivityFilter(environment_id=ENV_B), {3, 5, 9}, id="other-env"),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, category="automation"),
        {7, 8},
        id="env-category-automation",
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, category="user_action"),
        {1, 2, 4, 6},
        id="env-category-user",
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, target_type="environment"),
        {4, 6},
        id="env-target-type-environment",
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, target_type="container"),
        {1, 2, 7, 8},
        id="env-target-type-container",
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, action="environment_updated"), {4}, id="env-action"
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, actor_user_id=STANDARD_USER), {2}, id="env-user"
    ),
    pytest.param(ActivityFilter(environment_id=ENV_A, rule_id=RULE), {7, 8}, id="env-rule"),
    pytest.param(ActivityFilter(environment_id=ENV_A, job_run_id=FIRST_RUN), {7, 8}, id="env-run"),
    pytest.param(ActivityFilter(environment_id=ENV_A, result="failure"), {2, 8}, id="env-result"),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, occurred_from=moment(6)),
        {6, 7, 8},
        id="env-time",
    ),
    pytest.param(
        ActivityFilter(environment_id=ENV_A, target_type="container", target_id="c1"),
        {1, 7},
        id="object-in-env",
    ),
    pytest.param(
        ActivityFilter(target_type="environment", target_id="1"),
        {4},
        id="environment-target-with-no-environment",
    ),
]


@pytest.fixture(scope="module")
def seeded(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated database holding a fixed set of events; tests only read it."""
    path = tmp_path_factory.mktemp("activity-filters") / "dockmin.db"
    provision(migrated_template, path, with_sessions=False)
    asyncio.run(seed_world(path))
    connection = connect(path)
    try:
        connection.execute(
            "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, "
            "updated_at) VALUES ('updates', ?, '{}', ?, ?)",
            (UPDATER, stamp(0), stamp(0)),
        )
        connection.execute(
            "INSERT INTO scheduled_job (subsystem, rule_id, schedule_kind, schedule_expression, "
            "created_at, updated_at) VALUES (?, ?, 'interval', '3600', ?, ?)",
            (UPDATER, RULE, stamp(0), stamp(0)),
        )
        connection.executemany(
            "INSERT INTO job_run (id, scheduled_job_id, trigger, started_at) "
            "VALUES (?, 1, 'schedule', ?)",
            [(FIRST_RUN, stamp(0)), (SECOND_RUN, stamp(0))],
        )
        connection.executemany(INSERT_EVENT, seeded_rows())
    finally:
        connection.close()
    return path


@pytest.fixture
async def seeded_db(seeded: Path) -> AsyncIterator[Session]:
    async with Database(seeded).session() as opened:
        yield opened


@pytest.mark.parametrize(("criteria", "expected"), FILTER_CASES)
async def test_each_filter_returns_exactly_the_matching_events(
    seeded_db: Session, criteria: ActivityFilter, expected: set[int]
) -> None:
    page = await ActivityEventRepository(seeded_db).page(criteria, limit=500)

    assert {event.id for event in page.items} == expected
    assert page.next_cursor is None


async def test_an_environment_filter_lists_both_arms_newest_first(seeded_db: Session) -> None:
    page = await ActivityEventRepository(seeded_db).page(
        ActivityFilter(environment_id=ENV_A), limit=500
    )

    stamps = [event.occurred_at for event in page.items]
    assert stamps == sorted(stamps, reverse=True)


INSERT_BURST = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
    "INSERT INTO activity_event (category, actor_type, environment_id, target_type, target_id, "
    "action, result, occurred_at) SELECT 'user_action', 'anonymous', "
    "CASE WHEN i % 2 = 0 THEN ? END, CASE WHEN i % 2 = 0 THEN 'container' ELSE 'environment' END, "
    "CASE WHEN i % 2 = 0 THEN 'c' ELSE ? END, 'burst', 'success', ? FROM n"
)


async def test_paging_an_environment_through_events_that_share_a_timestamp_lists_each_once(
    database: Database,
) -> None:
    async with database.session() as db:
        await db.execute(
            "INSERT INTO environment (id, name, connection_kind, socket_path, created_at, "
            "updated_at) VALUES (?, 'burst', 'unix_socket', '/s', ?, ?)",
            (ENV_A, stamp(0), stamp(0)),
        )
        await db.execute(INSERT_BURST, (BURST_SIZE, ENV_A, str(ENV_A), BURST_STAMP))
        repository = ActivityEventRepository(db)

        walk = await walk_pages(
            lambda *, limit, cursor: repository.page(
                ActivityFilter(environment_id=ENV_A), limit=limit, cursor=cursor
            ),
            limit=BURST_PAGE,
            key=lambda event: event.id,
        )

    assert walk.keys == list(range(BURST_SIZE, 0, -1))
    assert walk.page_sizes == [BURST_PAGE] * 4 + [BURST_SIZE - 4 * BURST_PAGE]


@pytest.mark.parametrize("limit", [0, 501])
async def test_an_environment_page_outside_the_page_size_range_is_refused(
    seeded_db: Session, limit: int
) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        await ActivityEventRepository(seeded_db).page(
            ActivityFilter(environment_id=ENV_A), limit=limit
        )
