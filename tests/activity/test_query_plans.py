"""Query plans for the read side and for the foreign-key lookups that delete a parent row."""

import re
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db.engine import connect
from app.db.repositories.activity_event import ActivityEventRepository, ActivityFilter
from app.db.repositories.pagination import CursorPosition, encode_cursor
from tests.activity.support import RecordingSession

PAGE = 50
CURSOR = encode_cursor(CursorPosition("2030-01-01T00:00:00Z", 5))
FROM = datetime(2030, 1, 1, tzinfo=UTC)
TO = datetime(2030, 2, 1, tzinfo=UTC)
BARE_SCAN = re.compile(r"SCAN activity_event(?! USING)")
# SQLite prints the keyset bound as occurred_at<? or, in newer releases, as a row value.
KEYSET_BOUND = re.compile(r"occurred_at<\?|\(occurred_at,rowid\)<\(\?,\?\)")

OCCURRED_AT = "idx_activity_event_occurred_at"
CATEGORY = "idx_activity_event_category"
TARGET = "idx_activity_event_target"
ENVIRONMENT = "idx_activity_event_environment"
RULE = "idx_activity_event_rule_id"
USER = "idx_activity_event_actor_user_id"
SUBSYSTEM = "idx_activity_event_actor_subsystem"
ACTION = "idx_activity_event_action"
JOB_RUN = "idx_activity_event_job_run_id"

READ_PLANS = [
    pytest.param(ActivityFilter(), OCCURRED_AT, id="no-filter"),
    pytest.param(ActivityFilter(actor_type="user"), OCCURRED_AT, id="actor-type"),
    pytest.param(ActivityFilter(result="failure"), OCCURRED_AT, id="result"),
    pytest.param(ActivityFilter(category="authentication"), CATEGORY, id="category"),
    pytest.param(ActivityFilter(actor_user_id=1), USER, id="user"),
    pytest.param(ActivityFilter(actor_subsystem="container-updater"), SUBSYSTEM, id="subsystem"),
    pytest.param(ActivityFilter(rule_id=1), RULE, id="rule"),
    pytest.param(ActivityFilter(environment_id=1), ENVIRONMENT, id="environment"),
    pytest.param(ActivityFilter(target_type="user", target_id="1"), TARGET, id="target"),
    pytest.param(
        ActivityFilter(target_type="container", target_id="c", environment_id=1),
        TARGET,
        id="target-in-environment",
    ),
    pytest.param(ActivityFilter(occurred_from=FROM, occurred_to=TO), OCCURRED_AT, id="time-range"),
    pytest.param(ActivityFilter(occurred_from=FROM), OCCURRED_AT, id="time-from"),
    pytest.param(ActivityFilter(actor_user_id=1, category="user_action"), USER, id="user-category"),
    pytest.param(
        ActivityFilter(environment_id=1, category="user_action"),
        ENVIRONMENT,
        id="environment-category",
    ),
    pytest.param(
        ActivityFilter(environment_id=1, occurred_from=FROM), ENVIRONMENT, id="environment-time"
    ),
    pytest.param(ActivityFilter(actor_user_id=1, occurred_from=FROM), USER, id="user-time"),
    pytest.param(
        ActivityFilter(actor_subsystem="container-updater", result="failure"),
        SUBSYSTEM,
        id="subsystem-result",
    ),
    pytest.param(ActivityFilter(rule_id=1, result="failure"), RULE, id="rule-result"),
    pytest.param(
        ActivityFilter(category="authentication", occurred_from=FROM), CATEGORY, id="category-time"
    ),
    pytest.param(ActivityFilter(action="container_removed"), ACTION, id="action"),
    pytest.param(
        ActivityFilter(action="container_removed", occurred_from=FROM), ACTION, id="action-time"
    ),
    pytest.param(
        ActivityFilter(environment_id=1, action="container_removed"),
        TARGET,
        id="environment-action",
    ),
    pytest.param(ActivityFilter(environment_id=1, actor_user_id=1), TARGET, id="environment-user"),
    pytest.param(ActivityFilter(environment_id=1, rule_id=1), TARGET, id="environment-rule"),
    pytest.param(
        ActivityFilter(environment_id=1, target_type="container"),
        ENVIRONMENT,
        id="environment-target-type",
    ),
    pytest.param(
        ActivityFilter(environment_id=1, target_type="environment"),
        TARGET,
        id="environment-own-target-type",
    ),
    pytest.param(
        ActivityFilter(environment_id=1, target_type="stack", target_id="web"),
        TARGET,
        id="stack-panel",
    ),
]
JOB_RUN_PLANS = [
    pytest.param(ActivityFilter(job_run_id=1), id="job-run"),
    pytest.param(ActivityFilter(job_run_id=1, category="automation"), id="job-run-category"),
    pytest.param(ActivityFilter(job_run_id=1, occurred_from=FROM), id="job-run-time"),
    pytest.param(ActivityFilter(environment_id=1, job_run_id=1), id="job-run-environment"),
    pytest.param(
        ActivityFilter(environment_id=1, target_type="container", job_run_id=1),
        id="job-run-environment-target-type",
    ),
]
CUTOFF = "2030-01-01T00:00:00Z"
RETENTION_RESUMES = [
    pytest.param(None, id="first-batch"),
    pytest.param((CUTOFF, 5), id="later-batch"),
]
CURSORS = [pytest.param(None, id="first-page"), pytest.param(CURSOR, id="later-page")]

FOREIGN_KEY_LOOKUPS = [
    pytest.param("DELETE FROM automation_rule WHERE id = ?", RULE, id="rule"),
    pytest.param("DELETE FROM job_run WHERE id = ?", "idx_activity_event_job_run_id", id="job-run"),
    pytest.param("DELETE FROM environment WHERE id = ?", ENVIRONMENT, id="environment"),
    pytest.param("DELETE FROM user WHERE id = ?", USER, id="user"),
]


@pytest.fixture(scope="module")
def plans(
    migrated_template: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[sqlite3.Connection]:
    """An empty migrated database, which has no statistics, as the application's has."""
    path = tmp_path_factory.mktemp("plans") / "dockmin.db"
    shutil.copyfile(migrated_template, path)
    connection = connect(path)
    yield connection
    connection.close()


def explain(connection: sqlite3.Connection, sql: str, params: list[object]) -> str:
    rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(row["detail"] for row in rows)


def test_the_planner_has_no_statistics_to_work_from(plans: sqlite3.Connection) -> None:
    tables = plans.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'"
    ).fetchall()

    assert tables == []


@pytest.mark.parametrize("cursor", CURSORS)
@pytest.mark.parametrize(("criteria", "index"), READ_PLANS)
async def test_every_filter_is_served_by_its_index_in_order_with_no_sort(
    plans: sqlite3.Connection,
    recording: RecordingSession,
    criteria: ActivityFilter,
    index: str,
    cursor: str,
) -> None:
    await ActivityEventRepository(recording).page(criteria, limit=PAGE, cursor=cursor)  # type: ignore[arg-type]
    [(sql, params)] = recording.queries

    plan = explain(plans, sql, params)

    assert "USE TEMP B-TREE" not in plan
    assert BARE_SCAN.search(plan) is None
    assert index in plan
    assert cursor is None or KEYSET_BOUND.search(plan) is not None


@pytest.mark.parametrize("cursor", CURSORS)
@pytest.mark.parametrize("criteria", JOB_RUN_PLANS)
async def test_a_filter_naming_a_job_run_starts_from_the_run_index(
    plans: sqlite3.Connection,
    recording: RecordingSession,
    criteria: ActivityFilter,
    cursor: str,
) -> None:
    await ActivityEventRepository(recording).page(criteria, limit=PAGE, cursor=cursor)  # type: ignore[arg-type]
    [(sql, params)] = recording.queries

    plan = explain(plans, sql, params)

    assert BARE_SCAN.search(plan) is None
    assert f"USING INDEX {JOB_RUN} (job_run_id=?)" in plan


@pytest.mark.parametrize("resume", RETENTION_RESUMES)
async def test_retention_reads_its_candidates_in_order_from_the_time_index(
    plans: sqlite3.Connection, recording: RecordingSession, resume: tuple[str, int] | None
) -> None:
    recording.in_transaction = True

    await ActivityEventRepository(recording).prune_batch(  # type: ignore[arg-type]
        cutoff=CUTOFF, anchors=[], resume_after=resume, scan=PAGE, limit=PAGE
    )
    *_, (sql, params) = recording.queries

    plan = explain(plans, sql, params)
    assert "USE TEMP B-TREE" not in plan
    assert f"USING INDEX {OCCURRED_AT}" in plan


@pytest.mark.parametrize(("statement", "index"), FOREIGN_KEY_LOOKUPS)
def test_deleting_a_parent_row_finds_its_events_through_an_index(
    plans: sqlite3.Connection, statement: str, index: str
) -> None:
    plan = explain(plans, statement, [1])

    assert f"SEARCH activity_event USING COVERING INDEX {index}" in plan
