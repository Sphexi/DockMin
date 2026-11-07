"""Every filter's first page stays fast against a large, skewed table."""

import random
import shutil
import time
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.engine import Database, Session, connect
from app.db.repositories.activity_event import ActivityEventRepository, ActivityFilter
from app.db.repositories.clocked import format_timestamp

ROW_COUNT = 20_000
PAGE = 50
TIME_BUDGET_SECONDS = 0.25
USER_COUNT = 300
ENVIRONMENT_COUNT = 300
RULE_COUNT = 20
SUBSYSTEM_SHARE = 0.01
ANONYMOUS_ROWS = 10
SEED = 20260101
START = datetime(2030, 1, 1, tzinfo=UTC)
MIDDLE = START + timedelta(seconds=ROW_COUNT // 2)
SUBSYSTEMS = (
    "container-updater",
    "container-health-checker",
    "stack-restarter",
    "backup-syncer",
    "activity-log-exporter",
)
TARGET_TYPES = ("container", "volume", "stack", "user", "team")
STAMP = "2030-01-01T00:00:00Z"

INSERT_USER = (
    "INSERT INTO user (username, password_hash, role, created_at) VALUES (?, 'x', 'standard', ?)"
)
INSERT_ENVIRONMENT = (
    "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, updated_at) "
    "VALUES (?, 'standalone', 'unix_socket', ?, ?, ?)"
)
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, created_at, updated_at) "
    "VALUES (?, ?, '{}', ?, ?)"
)
INSERT_EVENT = (
    "INSERT INTO activity_event (category, actor_type, actor_user_id, actor_subsystem, rule_id, "
    "environment_id, target_type, target_id, action, result, occurred_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'probe', ?, ?)"
)

FIRST_PAGES = [
    ActivityFilter(),
    ActivityFilter(actor_type="user"),
    ActivityFilter(actor_type="subsystem"),
    ActivityFilter(result="failure"),
    ActivityFilter(category="authentication"),
    ActivityFilter(category="automation"),
    ActivityFilter(actor_user_id=7),
    ActivityFilter(actor_subsystem="container-updater"),
    ActivityFilter(rule_id=3),
    ActivityFilter(environment_id=11),
    ActivityFilter(target_type="container", target_id="t3", environment_id=11),
    ActivityFilter(occurred_from=MIDDLE, occurred_to=MIDDLE + timedelta(hours=1)),
    ActivityFilter(actor_user_id=7, category="user_action"),
    ActivityFilter(environment_id=11, occurred_from=MIDDLE),
    ActivityFilter(actor_subsystem="container-updater", result="failure"),
    ActivityFilter(rule_id=3, result="failure"),
    ActivityFilter(category="authentication", occurred_from=MIDDLE),
]


def event_rows(generator: random.Random) -> Iterable[tuple[object, ...]]:
    """Yield mostly user events, about one percent subsystem events and a few anonymous ones."""
    for index in range(ROW_COUNT):
        stamp = format_timestamp(START + timedelta(seconds=index // 2))
        result = "failure" if generator.random() < 0.05 else "success"
        target_id = f"t{generator.randrange(50)}"
        target_type = generator.choice(TARGET_TYPES)
        environment_id = generator.choice([None, generator.randint(1, ENVIRONMENT_COUNT)])
        if index < ANONYMOUS_ROWS:
            yield (
                "authentication",
                "anonymous",
                None,
                None,
                None,
                None,
                "user",
                None,
                result,
                stamp,
            )
        elif generator.random() < SUBSYSTEM_SHARE:
            rule_id = generator.randint(1, RULE_COUNT)
            subsystem = SUBSYSTEMS[(rule_id - 1) % len(SUBSYSTEMS)]
            yield (
                "automation",
                "subsystem",
                None,
                subsystem,
                rule_id,
                environment_id,
                target_type,
                target_id,
                result,
                stamp,
            )
        else:
            yield (
                "user_action",
                "user",
                generator.randint(1, USER_COUNT),
                None,
                None,
                environment_id,
                target_type,
                target_id,
                result,
                stamp,
            )


async def duration_of_first_pages(db: Session, criteria: Iterable[ActivityFilter]) -> float:
    repository = ActivityEventRepository(db)
    started = time.perf_counter()
    for each in criteria:
        await repository.page(each, limit=PAGE)
    return time.perf_counter() - started


@pytest.fixture(scope="module")
def large_table(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated database holding ROW_COUNT events, built once for the module."""
    path = tmp_path_factory.mktemp("large") / "dockmin.db"
    shutil.copyfile(migrated_template, path)
    connection = connect(path)
    try:
        with connection:
            connection.execute("BEGIN")
            connection.executemany(
                INSERT_USER, [(f"user{n}", STAMP) for n in range(1, USER_COUNT + 1)]
            )
            connection.executemany(
                INSERT_ENVIRONMENT,
                [(f"env{n}", f"/s{n}", STAMP, STAMP) for n in range(1, ENVIRONMENT_COUNT + 1)],
            )
            connection.executemany(
                INSERT_RULE,
                [
                    (f"rule{n}", SUBSYSTEMS[(n - 1) % len(SUBSYSTEMS)], STAMP, STAMP)
                    for n in range(1, RULE_COUNT + 1)
                ],
            )
            connection.executemany(INSERT_EVENT, event_rows(random.Random(SEED)))
            connection.execute("COMMIT")
    finally:
        connection.close()
    return path


@pytest.fixture
async def large_db(large_table: Path) -> AsyncIterator[Session]:
    async with Database(large_table).session() as opened:
        yield opened


async def test_the_first_page_of_every_filter_comes_back_quickly_from_a_large_table(
    large_db: Session,
) -> None:
    elapsed = await duration_of_first_pages(large_db, FIRST_PAGES)

    assert elapsed < TIME_BUDGET_SECONDS
