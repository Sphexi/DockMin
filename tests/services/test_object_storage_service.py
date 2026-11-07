"""Opening a stored target with its secret, and the keys the activity export writes."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.crypto.envelope import SecretLocation
from app.crypto.registry import OBJECT_STORAGE_SECRET_ACCESS_KEY
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.object_storage_target import ObjectStorageTargetRepository
from app.services.object_storage import (
    BACKUP_KEY_NAME,
    CredentialUnreadable,
    ObjectStorageTargets,
    activity_log_key,
    backup_folder,
    backup_object_key,
    service_log_folder,
    service_log_key,
)

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
OTHER_ROW_ID = 99
INSERT_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
    "activity_log_prefix, retention_count, created_at, updated_at) VALUES (?, "
    "'https://s3.example.com', 'eu-west-1', 'logs', 'AKIAEXAMPLE', ?, 0, 1, NULL, 'backups', "
    "'exports', NULL, 'x', 'x')"
)
RULE_CREATED = "2026-03-04T05:06:07Z"
FIRST_AT = "2026-01-02T03:04:05Z"
FIRST_ID = 12
FOLDER = "rule-7-20260304T050607/2026/01/02/00000000000000000012.csv.gz"
SERVICE_ID = "s" + "0" * 23 + "1"
TASK_ID = "t" + "0" * 23 + "2"
FIRST_NS = 1_767_323_045_000_000_007
SERVICE_STEM = f"logs/service-logs/rule-7-20260304T050607/3/web-{SERVICE_ID[:12]}"

KEYS = [
    pytest.param("exports", f"exports/{FOLDER}", id="prefix"),
    pytest.param("exports/", f"exports/{FOLDER}", id="prefix-with-trailing-slash"),
    pytest.param("a/b/c", f"a/b/c/{FOLDER}", id="nested-prefix"),
    pytest.param("", FOLDER, id="no-prefix"),
    pytest.param("/", FOLDER, id="only-a-slash"),
]
INVALID_PREFIXES = ["a//b", "a b", "../x", "é", "a/./b", "x" * 1100]


async def add_target(db: Session, vault: Vault, *, sealed: bool = True) -> int:
    inserted = await db.execute(INSERT_TARGET, ("primary", "placeholder"))
    target_id = inserted.last_row_id
    assert target_id is not None
    location = SecretLocation(
        OBJECT_STORAGE_SECRET_ACCESS_KEY.table, OBJECT_STORAGE_SECRET_ACCESS_KEY.column, target_id
    )
    ciphertext = vault.seal(SECRET, location) if sealed else "v1:AAAA:BBBB"
    await db.execute(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
        (ciphertext, target_id),
    )
    return target_id


@pytest.mark.parametrize(("prefix", "expected"), KEYS)
def test_the_key_names_the_rule_with_its_creation_time_the_day_and_the_first_id(
    prefix: str, expected: str
) -> None:
    assert activity_log_key(prefix, 7, RULE_CREATED, FIRST_ID, FIRST_AT) == expected


def test_a_rule_that_reuses_an_id_at_another_time_gets_another_folder() -> None:
    earlier = activity_log_key("p", 7, RULE_CREATED, FIRST_ID, FIRST_AT)
    later = activity_log_key("p", 7, "2026-03-04T05:06:08Z", FIRST_ID, FIRST_AT)

    assert earlier != later


def test_the_day_in_the_key_is_the_utc_day_of_the_first_event() -> None:
    key = activity_log_key("p", 7, RULE_CREATED, FIRST_ID, "2026-12-31T23:59:59Z")

    assert "/2026/12/31/" in key


@pytest.mark.parametrize("prefix", INVALID_PREFIXES)
def test_a_prefix_that_does_not_form_a_valid_key_is_a_value_error(prefix: str) -> None:
    with pytest.raises(ValueError, match="not a valid key prefix"):
        activity_log_key(prefix, 7, RULE_CREATED, FIRST_ID, FIRST_AT)


def test_a_service_log_key_names_rule_environment_service_task_day_and_first_line() -> None:
    key = service_log_key("logs", 7, RULE_CREATED, 3, "web", SERVICE_ID, TASK_ID, FIRST_NS)

    assert key == f"{SERVICE_STEM}/{TASK_ID}/2026/01/02/{FIRST_NS:020d}.ndjson.gz"


def test_a_service_log_key_for_lines_naming_no_task_uses_the_no_task_folder() -> None:
    key = service_log_key("logs", 7, RULE_CREATED, 3, "web", SERVICE_ID, None, FIRST_NS)

    assert key.startswith(f"{SERVICE_STEM}/no-task/2026/01/02/")


def test_a_service_log_key_continuing_a_timestamp_carries_the_count_in_its_name() -> None:
    key = service_log_key("logs", 7, RULE_CREATED, 3, "web", SERVICE_ID, TASK_ID, FIRST_NS, 4)

    assert key.endswith(f"/{FIRST_NS:020d}-4.ndjson.gz")


def test_a_service_name_outside_the_key_alphabet_is_written_with_underscores() -> None:
    folder = service_log_folder("logs", 7, RULE_CREATED, 3, "we b/é", SERVICE_ID, TASK_ID)

    assert f"/we_b__-{SERVICE_ID[:12]}/" in folder


@pytest.mark.parametrize("prefix", INVALID_PREFIXES)
def test_a_service_log_key_under_an_invalid_prefix_is_a_value_error(prefix: str) -> None:
    with pytest.raises(ValueError, match="not a valid key prefix"):
        service_log_key(prefix, 7, RULE_CREATED, 3, "web", SERVICE_ID, TASK_ID, FIRST_NS)


async def test_a_target_opens_with_its_secret_and_hides_it_from_its_repr(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault)

    opened = await ObjectStorageTargets(db, vault).open(target_id)

    assert opened is not None
    assert opened.credentials.secret_access_key.get_secret_value() == SECRET
    assert (opened.name, opened.activity_log_prefix) == ("primary", "exports")
    assert (opened.endpoint.bucket, opened.endpoint.use_path_style) == ("logs", False)
    assert SECRET not in repr(opened) and "AKIAEXAMPLE" not in repr(opened)


async def test_a_target_that_does_not_exist_opens_as_none_and_has_no_prefix(
    db: Session, vault: Vault
) -> None:
    targets = ObjectStorageTargets(db, vault)

    assert await targets.open(OTHER_ROW_ID) is None
    assert await targets.prefix_of(OTHER_ROW_ID) is None


async def test_the_prefix_is_read_without_opening_the_secret(db: Session, vault: Vault) -> None:
    target_id = await add_target(db, vault, sealed=False)

    assert await ObjectStorageTargets(db, vault).prefix_of(target_id) == ("primary", "exports")


async def test_a_secret_that_cannot_be_decrypted_is_reported_without_its_text(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault, sealed=False)

    with pytest.raises(CredentialUnreadable) as raised:
        await ObjectStorageTargets(db, vault).open(target_id)

    assert "v1:AAAA" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True


async def test_a_secret_sealed_for_another_row_is_not_readable_here(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault)
    moved = vault.seal(
        SECRET,
        SecretLocation(
            OBJECT_STORAGE_SECRET_ACCESS_KEY.table,
            OBJECT_STORAGE_SECRET_ACCESS_KEY.column,
            OTHER_ROW_ID,
        ),
    )
    await db.execute(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ? WHERE id = ?",
        (moved, target_id),
    )

    with pytest.raises(CredentialUnreadable):
        await ObjectStorageTargets(db, vault).open(target_id)


async def test_the_repository_lists_names_by_name_and_keeps_the_secret_out_of_its_repr(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault)
    repository = ObjectStorageTargetRepository(db)

    stored = await repository.get(target_id)
    summaries = await repository.summaries(limit=10)

    assert summaries == [(target_id, "primary")]
    assert stored is not None
    assert SECRET not in repr(stored) and stored.secret_access_key_ciphertext not in repr(stored)
    assert await repository.get(OTHER_ROW_ID) is None


BACKUP_MOMENT = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
BACKUP_NAME = "dockmin-backup-20260304T050607Z.tar.gz"
BACKUP_FOLDER = "rule-7-20260304T050607"

BACKUP_KEYS = [
    pytest.param("backups", f"backups/{BACKUP_FOLDER}", id="prefix"),
    pytest.param("backups/", f"backups/{BACKUP_FOLDER}", id="prefix-with-trailing-slash"),
    pytest.param("a/b/c", f"a/b/c/{BACKUP_FOLDER}", id="nested-prefix"),
    pytest.param("", BACKUP_FOLDER, id="no-prefix"),
]


@pytest.mark.parametrize(("prefix", "folder"), BACKUP_KEYS)
def test_a_rules_backup_folder_carries_its_id_and_creation_time_under_the_prefix(
    prefix: str, folder: str
) -> None:
    assert backup_folder(prefix, 7, RULE_CREATED) == folder
    assert backup_object_key(prefix, 7, RULE_CREATED, BACKUP_MOMENT) == f"{folder}/{BACKUP_NAME}"


def test_a_rule_that_reuses_an_id_at_another_time_gets_another_backup_folder() -> None:
    assert backup_folder("p", 7, RULE_CREATED) != backup_folder("p", 7, "2026-03-04T05:06:08Z")


def test_the_archive_name_is_the_utc_time_whatever_zone_the_moment_is_in() -> None:
    moment = datetime(2026, 3, 4, 7, 6, 7, tzinfo=timezone(timedelta(hours=2)))

    assert backup_object_key("p", 7, RULE_CREATED, moment).endswith(BACKUP_NAME)


@pytest.mark.parametrize("prefix", INVALID_PREFIXES)
def test_a_prefix_that_does_not_form_a_valid_key_is_a_value_error_for_backups(
    prefix: str,
) -> None:
    with pytest.raises(ValueError, match="not a valid key prefix"):
        backup_object_key(prefix, 7, RULE_CREATED, BACKUP_MOMENT)


@pytest.mark.parametrize(
    ("name", "matches"),
    [
        (BACKUP_NAME, True),
        ("dockmin-backup-20260304T050607Z.tar.gz.part", False),
        ("dockmin-backup-bad.tar.gz", False),
        ("dockmin-backup-2026030T050607Z.tar.gz", False),
        ("sub/dockmin-backup-20260304T050607Z.tar.gz", False),
        ("notes.txt", False),
    ],
)
def test_only_the_names_the_syncer_writes_are_treated_as_archives(name: str, matches: bool) -> None:
    assert (BACKUP_KEY_NAME.fullmatch(name) is not None) is matches


async def test_the_backup_prefix_and_retention_are_read_without_opening_the_secret(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault, sealed=False)
    await db.execute("UPDATE object_storage_target SET retention_count = 4")
    targets = ObjectStorageTargets(db, vault)

    assert await targets.backup_prefix_of(target_id) == ("primary", "backups", 4)
    assert await targets.backup_prefix_of(OTHER_ROW_ID) is None


async def test_an_opened_target_carries_the_backup_prefix_and_retention(
    db: Session, vault: Vault
) -> None:
    target_id = await add_target(db, vault)

    opened = await ObjectStorageTargets(db, vault).open(target_id)

    assert opened is not None
    assert (opened.backup_prefix, opened.retention_count) == ("backups", None)
