"""Migration 0013 and the repository that is the only writer of the image update status table."""

import sqlite3
from collections.abc import AsyncIterator

import pytest

from app.db.engine import Database
from app.db.migrations import MIGRATIONS_DIR
from app.db.repositories.image_update_status import (
    MAX_LOOKUP_REFERENCES,
    ImageUpdateStatusRepository,
    StoredUpdateStatus,
)
from tests.db.helpers import CREATED_STAMP, LATER_STAMP

CHECK_FAILED = "CHECK constraint failed"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
REFERENCE = "registry.example.com/app:1"
BEFORE_ALL = "2000-01-01T00:00:00Z"
INSERT = (
    "INSERT INTO image_update_status (environment_id, image_reference, state, reason, "
    "local_digest, remote_digest, checked_at) "
    "VALUES (:environment_id, :image_reference, :state, :reason, :local_digest, "
    ":remote_digest, :checked_at)"
)
UP_TO_DATE = {
    "environment_id": 1,
    "image_reference": REFERENCE,
    "state": "up_to_date",
    "reason": None,
    "local_digest": DIGEST,
    "remote_digest": DIGEST,
    "checked_at": CREATED_STAMP,
}
UNKNOWN = {**UP_TO_DATE, "state": "unknown", "reason": "timeout", "remote_digest": None}
REFUSED = [
    pytest.param({"state": "stale"}, id="unknown-state"),
    pytest.param({"image_reference": ""}, id="empty-reference"),
    pytest.param({"image_reference": "r" * 513}, id="long-reference"),
    pytest.param({"reason": "timeout"}, id="reason-on-a-definite-state"),
    pytest.param({**UNKNOWN, "reason": None}, id="unknown-without-a-reason"),
    pytest.param({**UNKNOWN, "reason": "Timed out"}, id="reason-outside-the-code-alphabet"),
    pytest.param({**UNKNOWN, "reason": "r" * 65}, id="long-reason"),
    pytest.param({"remote_digest": None}, id="up-to-date-without-a-remote-digest"),
    pytest.param(
        {"state": "update_available", "remote_digest": None}, id="update-without-a-remote-digest"
    ),
    pytest.param({"local_digest": "sha256:" + "a" * 63}, id="short-local-digest"),
    pytest.param({"remote_digest": "sha256:" + "A" * 64}, id="upper-case-remote-digest"),
    pytest.param({"checked_at": "2026-01-02 03:04:05"}, id="checked-at-in-another-form"),
]


@pytest.fixture
def seeded(raw_connection: sqlite3.Connection) -> sqlite3.Connection:
    for name in ("one", "two"):
        raw_connection.execute(
            "INSERT INTO environment (name, connection_kind, socket_path, created_at, updated_at) "
            "VALUES (?, 'unix_socket', ?, ?, ?)",
            (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP),
        )
    return raw_connection


@pytest.fixture
async def repository(
    database: Database, seeded: sqlite3.Connection
) -> AsyncIterator[ImageUpdateStatusRepository]:
    async with database.session() as session:
        yield ImageUpdateStatusRepository(session)


def stored(**changes: str | None) -> StoredUpdateStatus:
    fields: dict[str, str | None] = {
        "reference": REFERENCE,
        "state": "update_available",
        "reason": None,
        "local_digest": DIGEST,
        "remote_digest": OTHER_DIGEST,
        "checked_at": CREATED_STAMP,
    }
    return StoredUpdateStatus(**{**fields, **changes})  # type: ignore[arg-type]


def test_the_migration_is_the_thirteenth_and_is_recorded(seeded: sqlite3.Connection) -> None:
    names = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    versions = [row[0] for row in seeded.execute("SELECT version FROM schema_migration")]

    assert names[12] == "0013_image_update_status.sql"
    assert 13 in versions


@pytest.mark.parametrize("row", [UP_TO_DATE, UNKNOWN], ids=["definite", "unknown"])
def test_each_valid_shape_is_accepted(seeded: sqlite3.Connection, row: dict[str, object]) -> None:
    seeded.execute(INSERT, row)

    assert seeded.execute("SELECT count(*) FROM image_update_status").fetchone()[0] == 1


@pytest.mark.parametrize("overrides", REFUSED)
def test_every_check_refuses_its_case(
    seeded: sqlite3.Connection, overrides: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=CHECK_FAILED):
        seeded.execute(INSERT, {**UP_TO_DATE, **overrides})


def test_a_reference_has_one_row_per_environment_and_environments_are_independent(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute(INSERT, UP_TO_DATE)

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        seeded.execute(INSERT, UP_TO_DATE)
    seeded.execute(INSERT, {**UP_TO_DATE, "environment_id": 2})

    assert seeded.execute("SELECT count(*) FROM image_update_status").fetchone()[0] == 2


def test_removing_an_environment_removes_its_rows_only(seeded: sqlite3.Connection) -> None:
    seeded.execute(INSERT, UP_TO_DATE)
    seeded.execute(INSERT, {**UP_TO_DATE, "environment_id": 2})

    seeded.execute("DELETE FROM environment WHERE id = 1")

    remaining = seeded.execute("SELECT environment_id FROM image_update_status").fetchall()
    assert [row[0] for row in remaining] == [2]


async def test_an_upsert_replaces_a_row_with_a_newer_or_equal_answer_and_drops_an_older_one(
    repository: ImageUpdateStatusRepository,
) -> None:
    await repository.upsert(1, [stored()], prune_before=BEFORE_ALL)

    await repository.upsert(
        1,
        [stored(state="up_to_date", remote_digest=DIGEST, checked_at=LATER_STAMP)],
        prune_before=BEFORE_ALL,
    )
    await repository.upsert(1, [stored(checked_at=CREATED_STAMP)], prune_before=BEFORE_ALL)

    [row] = (await repository.for_references(1, [REFERENCE])).values()
    assert (row.state, row.remote_digest, row.checked_at) == ("up_to_date", DIGEST, LATER_STAMP)


async def test_an_upsert_deletes_only_the_environments_rows_checked_before_the_cutoff(
    repository: ImageUpdateStatusRepository,
) -> None:
    await repository.upsert(
        1, [stored(reference="old:1", checked_at=CREATED_STAMP)], prune_before=BEFORE_ALL
    )
    await repository.upsert(
        2, [stored(reference="old:1", checked_at=CREATED_STAMP)], prune_before=BEFORE_ALL
    )

    await repository.upsert(1, [stored(checked_at=LATER_STAMP)], prune_before=LATER_STAMP)

    assert sorted(await repository.for_references(1, ["old:1", REFERENCE])) == [REFERENCE]
    assert list(await repository.for_references(2, ["old:1"])) == ["old:1"]


async def test_lookup_returns_what_exists_for_references_beyond_one_statement(
    repository: ImageUpdateStatusRepository,
) -> None:
    await repository.upsert(1, [stored()], prune_before=BEFORE_ALL)
    asked = [f"absent-{number}:1" for number in range(MAX_LOOKUP_REFERENCES + 1)]

    found = await repository.for_references(1, [*asked, REFERENCE, REFERENCE])

    assert list(found) == [REFERENCE]
