"""Tests for app/db/repositories/stack_dismissal.py: keys, stored objects and the replace-on-put."""

import pytest

from app.db.engine import Session
from app.db.repositories.errors import MissingReferenceError
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.db.repositories.user import UserRepository
from tests.db.helpers import CREATED_AT, CREATED_STAMP, LATER_AT, LATER_STAMP, UNKNOWN_ID, at

FIRST_ENVIRONMENT, SECOND_ENVIRONMENT = 1, 2
PROJECT = "Shop"
FOLDED = "shop"
OBJECTS = ["volume:data:2026-01-01", "network:b", "network:a"]
SORTED_OBJECTS = ("network:a", "network:b", "volume:data:2026-01-01")
REPLACEMENT = ["config:c"]


@pytest.fixture
async def environments(session: Session) -> None:
    for name in ("one", "two"):
        await session.execute(
            "INSERT INTO environment (name, connection_kind, socket_path, created_at, "
            "updated_at) VALUES (?, 'unix_socket', ?, ?, ?)",
            (name, f"/run/{name}.sock", CREATED_STAMP, CREATED_STAMP),
        )


@pytest.fixture
def repo(session: Session, environments: None) -> StackDismissalRepository:
    return StackDismissalRepository(session, at(CREATED_AT))


@pytest.fixture
async def admin_id(session: Session) -> int:
    return (await UserRepository(session, at(CREATED_AT)).create("pat", "h", "administrator")).id


async def test_a_dismissal_is_stored_under_the_folded_name_with_sorted_unique_objects(
    repo: StackDismissalRepository, admin_id: int
) -> None:
    await repo.put_dismissal(
        FIRST_ENVIRONMENT, "compose", PROJECT, [*OBJECTS, OBJECTS[0]], admin_id
    )

    stored = await repo.dismissal(FIRST_ENVIRONMENT, "compose", PROJECT.upper())

    assert stored is not None
    assert stored.project_key == FOLDED
    assert stored.objects == SORTED_OBJECTS
    assert (stored.dismissed_by_user_id, stored.dismissed_by) == (admin_id, "pat")
    assert stored.dismissed_at == CREATED_STAMP


async def test_a_second_put_replaces_the_objects_the_user_and_the_time(
    repo: StackDismissalRepository, session: Session, admin_id: int
) -> None:
    later = StackDismissalRepository(session, at(LATER_AT))
    await repo.put_dismissal(FIRST_ENVIRONMENT, "swarm", PROJECT, OBJECTS, admin_id)

    await later.put_dismissal(FIRST_ENVIRONMENT, "swarm", FOLDED, REPLACEMENT, None)
    stored = await later.dismissals(FIRST_ENVIRONMENT, "swarm")

    assert [(s.project_key, s.objects, s.dismissed_by, s.dismissed_at) for s in stored] == [
        (FOLDED, tuple(REPLACEMENT), None, LATER_STAMP)
    ]


async def test_the_list_holds_one_kind_of_one_environment_ordered_by_key(
    repo: StackDismissalRepository,
) -> None:
    await repo.put_dismissal(FIRST_ENVIRONMENT, "compose", "zeta", REPLACEMENT, None)
    await repo.put_dismissal(FIRST_ENVIRONMENT, "compose", "Alpha", REPLACEMENT, None)
    await repo.put_dismissal(FIRST_ENVIRONMENT, "swarm", "swarm-only", REPLACEMENT, None)
    await repo.put_dismissal(SECOND_ENVIRONMENT, "compose", "elsewhere", REPLACEMENT, None)

    compose = await repo.dismissals(FIRST_ENVIRONMENT, "compose")
    swarm = await repo.dismissals(FIRST_ENVIRONMENT, "swarm")
    other = await repo.dismissals(SECOND_ENVIRONMENT, "compose")
    absent = await repo.dismissal(FIRST_ENVIRONMENT, "swarm", "zeta")

    assert [s.project_key for s in compose] == ["alpha", "zeta"]
    assert [s.project_key for s in swarm] == ["swarm-only"]
    assert [s.project_key for s in other] == ["elsewhere"]
    assert absent is None


async def test_dropping_a_dismissal_reports_whether_one_was_there_and_leaves_the_other_kind(
    repo: StackDismissalRepository,
) -> None:
    await repo.put_dismissal(FIRST_ENVIRONMENT, "compose", FOLDED, REPLACEMENT, None)
    await repo.put_dismissal(FIRST_ENVIRONMENT, "swarm", FOLDED, REPLACEMENT, None)

    dropped = await repo.drop_dismissal(FIRST_ENVIRONMENT, "compose", PROJECT)
    dropped_again = await repo.drop_dismissal(FIRST_ENVIRONMENT, "compose", PROJECT)

    assert (dropped, dropped_again) == (True, False)
    assert await repo.dismissal(FIRST_ENVIRONMENT, "compose", FOLDED) is None
    assert await repo.dismissal(FIRST_ENVIRONMENT, "swarm", FOLDED) is not None


async def test_an_unknown_environment_or_user_is_a_missing_reference_and_a_deleted_user_is_unnamed(
    repo: StackDismissalRepository, session: Session, admin_id: int
) -> None:
    with pytest.raises(MissingReferenceError):
        await repo.put_dismissal(UNKNOWN_ID, "compose", FOLDED, REPLACEMENT, None)
    with pytest.raises(MissingReferenceError):
        await repo.put_dismissal(FIRST_ENVIRONMENT, "compose", FOLDED, REPLACEMENT, UNKNOWN_ID)
    await repo.put_dismissal(FIRST_ENVIRONMENT, "compose", FOLDED, REPLACEMENT, admin_id)

    await session.execute("DELETE FROM user WHERE id = ?", (admin_id,))
    stored = await repo.dismissal(FIRST_ENVIRONMENT, "compose", FOLDED)

    assert stored is not None
    assert (stored.dismissed_by_user_id, stored.dismissed_by) == (None, None)
