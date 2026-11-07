"""Writing object storage targets: sealing, guarded changes, removal and referencing rules."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.registry import OBJECT_STORAGE_SECRET_ACCESS_KEY
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.object_storage_target import (
    ObjectStorageTargetRepository,
    ObjectStorageTargetStore,
    TargetFields,
)
from tests.crypto.helpers import FIRST_KEY, vault_with

SECRET = "s3cret-value"
OTHER_SECRET = "n3w-secret-value"
START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
FIRST_STAMP = "2026-03-01T12:00:00Z"
NEXT_SECOND = "2026-03-01T12:00:01Z"
LATER = "2026-03-01T12:05:00Z"
PAGE_SIZE = 2
UNKNOWN_ID = 99
INSERT_RULE = (
    "INSERT INTO automation_rule (name, subsystem, target_selector, parameters, created_at, "
    "updated_at) VALUES (?, ?, '{}', ?, 'x', 'x')"
)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def fields(name: str, **changes: object) -> TargetFields:
    values: dict[str, object] = {
        "name": name,
        "endpoint_url": "https://s3.example",
        "region": "us-east-1",
        "bucket": "b",
        "access_key_id": "AK",
        "use_path_style": True,
        "verify_tls": True,
        "ca_bundle": None,
        "backup_prefix": "backups",
        "activity_log_prefix": "logs",
        "retention_count": None,
    }
    return TargetFields(**(values | changes))  # type: ignore[arg-type]


@pytest.fixture
def vault() -> Vault:
    return vault_with(FIRST_KEY)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(session: Session, vault: Vault, clock: Clock) -> ObjectStorageTargetStore:
    return ObjectStorageTargetStore(session, vault, clock)


@pytest.fixture
def reads(session: Session) -> ObjectStorageTargetRepository:
    return ObjectStorageTargetRepository(session)


async def test_adding_a_target_seals_its_secret_at_its_own_location_and_stamps_both_times(
    store: ObjectStorageTargetStore, reads: ObjectStorageTargetRepository, vault: Vault
) -> None:
    target_id = await store.add_target(fields("Primary"), secret_access_key=SECRET)

    stored = await reads.get(target_id)

    assert stored is not None
    location = SecretLocation(
        OBJECT_STORAGE_SECRET_ACCESS_KEY.table, OBJECT_STORAGE_SECRET_ACCESS_KEY.column, target_id
    )
    assert vault.open(stored.secret_access_key_ciphertext, location) == SECRET
    with pytest.raises(DecryptionError):
        vault.open(
            stored.secret_access_key_ciphertext,
            SecretLocation(location.table, location.column, target_id + 1),
        )
    assert SECRET not in repr(stored)
    assert stored.created_at == stored.updated_at == FIRST_STAMP


async def test_a_name_taken_in_any_case_and_an_empty_secret_are_refused(
    store: ObjectStorageTargetStore,
) -> None:
    await store.add_target(fields("Primary"), secret_access_key=SECRET)

    with pytest.raises(DuplicateError):
        await store.add_target(fields("PRIMARY"), secret_access_key=SECRET)
    with pytest.raises(ValueError, match="secret"):
        await store.add_target(fields("empty"), secret_access_key="")


async def test_a_target_whose_two_prefixes_are_equal_is_refused_by_the_schema(
    store: ObjectStorageTargetStore,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_target(
            fields("same", activity_log_prefix="backups"), secret_access_key=SECRET
        )


async def test_changing_a_target_keeps_its_secret_unless_given_a_new_one_and_moves_the_stamp(
    store: ObjectStorageTargetStore,
    reads: ObjectStorageTargetRepository,
    clock: Clock,
    vault: Vault,
) -> None:
    target_id = await store.add_target(fields("Primary"), secret_access_key=SECRET)
    stored = await reads.get(target_id)
    assert stored is not None

    kept = await store.change_target(
        target_id,
        fields("Primary", region="eu-west-1"),
        secret_access_key=None,
        expected_updated_at=stored.updated_at,
    )
    after_keep = await reads.get(target_id)
    clock.now += timedelta(minutes=5)
    assert after_keep is not None
    replaced = await store.change_target(
        target_id,
        fields("Primary"),
        secret_access_key=OTHER_SECRET,
        expected_updated_at=after_keep.updated_at,
    )
    after_replace = await reads.get(target_id)

    assert after_keep is not None and after_replace is not None
    assert kept and after_keep.updated_at == NEXT_SECOND
    assert after_keep.secret_access_key_ciphertext == stored.secret_access_key_ciphertext
    assert replaced and after_replace.updated_at == LATER
    location = SecretLocation(
        OBJECT_STORAGE_SECRET_ACCESS_KEY.table, OBJECT_STORAGE_SECRET_ACCESS_KEY.column, target_id
    )
    assert vault.open(after_replace.secret_access_key_ciphertext, location) == OTHER_SECRET


async def test_a_change_from_a_stale_stamp_or_to_a_missing_target_changes_nothing(
    store: ObjectStorageTargetStore, reads: ObjectStorageTargetRepository
) -> None:
    target_id = await store.add_target(fields("Primary"), secret_access_key=SECRET)
    stored = await reads.get(target_id)
    assert stored is not None
    await store.change_target(
        target_id,
        fields("Primary", region="eu-west-1"),
        secret_access_key=None,
        expected_updated_at=stored.updated_at,
    )

    stale = await store.change_target(
        target_id, fields("Primary"), secret_access_key=None, expected_updated_at=stored.updated_at
    )
    missing = await store.change_target(
        UNKNOWN_ID, fields("x"), secret_access_key=None, expected_updated_at=stored.updated_at
    )

    current = await reads.get(target_id)
    assert (stale, missing) == (False, False)
    assert current is not None and current.region == "eu-west-1"


async def test_removing_a_target_needs_its_exact_name_and_leaves_records_with_no_target(
    store: ObjectStorageTargetStore, reads: ObjectStorageTargetRepository, session: Session
) -> None:
    target_id = await store.add_target(fields("Primary"), secret_access_key=SECRET)
    await session.execute(
        "INSERT INTO backup_record (object_storage_target_id, created_at, key_version, "
        "key_fingerprint, outcome) VALUES (?, 'x', 1, 'f', 'success')",
        (target_id,),
    )

    wrong_case = await store.remove_target(target_id, expected_name="primary")
    exact = await store.remove_target(target_id, expected_name="Primary")

    orphaned = await session.fetch_one(
        "SELECT count(*) AS total FROM backup_record WHERE object_storage_target_id IS NULL"
    )
    assert (wrong_case, exact) == (False, True)
    assert await reads.get(target_id) is None
    assert orphaned is not None and orphaned["total"] == 1


async def test_the_rules_naming_a_target_are_found_by_subsystem_and_by_name(
    store: ObjectStorageTargetStore, reads: ObjectStorageTargetRepository, session: Session
) -> None:
    target_id = await store.add_target(fields("Primary"), secret_access_key=SECRET)
    named = f'{{"object_storage_target_id":{target_id}}}'
    other = f'{{"object_storage_target_id":{target_id + 1}}}'
    await session.execute(INSERT_RULE, ("nightly", "backup-syncer", named))
    await session.execute(INSERT_RULE, ("export", "activity-log-exporter", named))
    await session.execute(INSERT_RULE, ("elsewhere", "backup-syncer", other))
    await session.execute(INSERT_RULE, ("restarts", "stack-restarter", named))

    found = await reads.rules_referencing(target_id, limit=10)
    none = await reads.rules_referencing(UNKNOWN_ID, limit=10)

    assert [(rule.name, rule.subsystem) for rule in found] == [
        ("export", "activity-log-exporter"),
        ("nightly", "backup-syncer"),
    ]
    assert none == []


async def test_targets_are_paged_by_name_without_repeats_or_gaps(
    store: ObjectStorageTargetStore, reads: ObjectStorageTargetRepository
) -> None:
    for index in range(5):
        await store.add_target(
            fields(f"target{index}", backup_prefix=f"b{index}"), secret_access_key=SECRET
        )
    names: list[str] = []
    cursor = None

    while True:
        page = await reads.page_targets(limit=PAGE_SIZE, cursor=cursor)
        names += [target.name for target in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break

    assert names == [f"target{index}" for index in range(5)]
