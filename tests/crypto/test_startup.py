"""Tests for startup verification in app/crypto/startup.py and the key-required launch."""

import base64
import hashlib
import shutil
from pathlib import Path

import pytest

from app.config import (
    ALLOW_KEY_ROTATION_VAR,
    DB_FILENAME,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_FILE_VAR,
    SECRET_KEY_VAR,
    Settings,
    load_settings,
)
from app.crypto.envelope import SecretLocation
from app.crypto.keys import MasterKeys
from app.crypto.registry import REGISTRY_CREDENTIAL_SECRET
from app.crypto.startup import CANARY_PLAINTEXT, KeyMismatchError, establish_vault
from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.crypto_state import CANARY_KEY, INSTANCE_ID_KEY, KEY_VERSION_KEY
from app.main import create_app
from tests.conftest import DATA_DIR_VAR
from tests.crypto.helpers import (
    CRYPTO_STATE_PATTERN,
    CRYPTO_STATE_ROWS,
    FIRST_KEY,
    FIRST_ROW,
    FIRST_VERSION,
    PLAINTEXT,
    SECOND_KEY,
    SECOND_VERSION,
    THIRD_KEY,
    TracingDatabase,
    add_credential,
    execute_sql,
    read_state,
    scalar,
    start,
)

SECRET = REGISTRY_CREDENTIAL_SECRET
SECRET_LOCATION = SecretLocation(SECRET.table, SECRET.column, FIRST_ROW)
CRYPTO_STATE_COUNT = "SELECT COUNT(*) FROM setting WHERE key LIKE ?"


def _directory_digest(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()
    }


@pytest.fixture
def no_key_in_the_environment(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    monkeypatch.delenv(SECRET_KEY_VAR, raising=False)
    monkeypatch.delenv(SECRET_KEY_FILE_VAR, raising=False)
    monkeypatch.setenv(DATA_DIR_VAR, str(data_dir))
    return data_dir


def test_building_the_application_without_a_key_writes_nothing_to_the_data_directory(
    no_key_in_the_environment: Path,
) -> None:
    before = _directory_digest(no_key_in_the_environment)

    application = create_app()

    assert application.state.settings.key_source == "generated"
    assert _directory_digest(no_key_in_the_environment) == before == {}


def test_building_the_application_without_a_key_leaves_an_existing_database_byte_for_byte_unchanged(
    no_key_in_the_environment: Path, migrated_template: Path
) -> None:
    shutil.copyfile(migrated_template, no_key_in_the_environment / DB_FILENAME)
    before = _directory_digest(no_key_in_the_environment)

    create_app()

    assert _directory_digest(no_key_in_the_environment) == before


async def test_first_start_on_an_empty_database_stores_the_three_state_rows(
    database: Database,
) -> None:
    vault = await start(database, FIRST_KEY)

    assert vault.current_version == FIRST_VERSION
    assert scalar(database, CRYPTO_STATE_COUNT, (CRYPTO_STATE_PATTERN,)) == CRYPTO_STATE_ROWS


async def test_first_start_stores_a_canary_that_the_vault_opens_at_its_own_location(
    database: Database,
) -> None:
    vault = await start(database, FIRST_KEY)

    state = await read_state(database)

    canary_location = SecretLocation("setting", "value", state.canary_row_id)
    assert vault.open(state.canary, canary_location) == CANARY_PLAINTEXT


async def test_restart_with_the_same_key_keeps_the_instance_id_and_version(
    database_at_version_one: Database,
) -> None:
    before = await read_state(database_at_version_one)

    await start(database_at_version_one, FIRST_KEY)

    assert await read_state(database_at_version_one) == before


async def test_start_with_the_wrong_key_names_the_variable_and_the_expected_version(
    database_at_version_one: Database,
) -> None:
    with pytest.raises(KeyMismatchError) as raised:
        await start(database_at_version_one, SECOND_KEY)

    message = str(raised.value)
    assert SECRET_KEY_VAR in message
    assert f"key version {FIRST_VERSION}" in message
    assert PREVIOUS_SECRET_KEY_VAR in message


GENERATED_KEY_DESCRIPTION = "the key in /key/secret.key"


async def test_a_mismatch_names_the_source_of_the_key_it_is_given(
    database_at_version_one: Database,
) -> None:
    with pytest.raises(KeyMismatchError) as raised:
        await start(database_at_version_one, SECOND_KEY, key_description=GENERATED_KEY_DESCRIPTION)

    assert str(raised.value).startswith(
        f"{GENERATED_KEY_DESCRIPTION} does not match this database, which expects key version "
        f"{FIRST_VERSION}"
    )
    assert SECRET_KEY_VAR not in str(raised.value)


async def test_a_refused_rotation_names_the_source_of_the_new_key_it_is_given(
    database_at_version_one: Database,
) -> None:
    with pytest.raises(KeyMismatchError) as raised:
        await start(
            database_at_version_one,
            SECOND_KEY,
            FIRST_KEY,
            key_description=GENERATED_KEY_DESCRIPTION,
        )

    message = str(raised.value)
    assert f"but {GENERATED_KEY_DESCRIPTION} does not." in message
    assert f"rewrite its canary under {GENERATED_KEY_DESCRIPTION};" in message
    assert ALLOW_KEY_ROTATION_VAR in message


async def test_start_with_the_wrong_key_runs_no_statement_that_touches_a_secret_column(
    database_at_version_one: Database, traced_database: TracingDatabase
) -> None:
    with pytest.raises(KeyMismatchError):
        await start(traced_database, SECOND_KEY)

    assert traced_database.statements
    assert traced_database.statements_naming_a_secret_column() == []


@pytest.mark.parametrize(
    "deleted_pattern", [KEY_VERSION_KEY, CANARY_KEY, INSTANCE_ID_KEY, CRYPTO_STATE_PATTERN]
)
async def test_deleted_state_rows_beside_stored_secrets_refuse_to_start_and_write_nothing(
    database_at_version_one: Database, deleted_pattern: str
) -> None:
    execute_sql(database_at_version_one, "DELETE FROM setting WHERE key LIKE ?", (deleted_pattern,))
    remaining = scalar(database_at_version_one, CRYPTO_STATE_COUNT, (CRYPTO_STATE_PATTERN,))

    with pytest.raises(KeyMismatchError, match="state rows are missing"):
        await start(database_at_version_one, SECOND_KEY)

    after = scalar(database_at_version_one, CRYPTO_STATE_COUNT, (CRYPTO_STATE_PATTERN,))
    assert after == remaining


async def test_empty_secret_columns_do_not_make_deleted_state_rows_a_refusal(
    database: Database,
) -> None:
    await start(database, FIRST_KEY)
    execute_sql(database, "DELETE FROM setting WHERE key LIKE ?", (CRYPTO_STATE_PATTERN,))

    vault = await start(database, SECOND_KEY)

    assert vault.current_version == FIRST_VERSION


@pytest.mark.parametrize("previous", [None, THIRD_KEY], ids=["no previous key", "wrong previous"])
async def test_start_that_cannot_read_values_left_at_an_earlier_version_refuses_naming_it(
    rotated_with_a_stranded_value: Database, previous: bytes | None
) -> None:
    with pytest.raises(KeyMismatchError) as raised:
        await start(rotated_with_a_stranded_value, SECOND_KEY, previous)

    assert f"key version {FIRST_VERSION} (1 values)" in str(raised.value)
    assert PREVIOUS_SECRET_KEY_VAR in str(raised.value)


async def test_start_with_the_correct_previous_key_succeeds_at_the_rotated_version(
    rotated_with_a_stranded_value: Database,
) -> None:
    vault = await start(rotated_with_a_stranded_value, SECOND_KEY, FIRST_KEY)

    assert vault.current_version == SECOND_VERSION


async def test_vault_from_a_start_with_the_correct_previous_key_reads_the_old_value(
    rotated_with_a_stranded_value: Database,
) -> None:
    vault = await start(rotated_with_a_stranded_value, SECOND_KEY, FIRST_KEY)
    stored = scalar(
        rotated_with_a_stranded_value, "SELECT secret_ciphertext FROM registry_credential"
    )

    opened = vault.open(stored, SECRET_LOCATION)

    assert opened == PLAINTEXT


async def test_rotation_without_the_opt_in_refuses_and_leaves_the_key_version_untouched(
    database_at_version_one: Database,
) -> None:
    before = await read_state(database_at_version_one)

    with pytest.raises(KeyMismatchError, match=ALLOW_KEY_ROTATION_VAR):
        await start(database_at_version_one, SECOND_KEY, FIRST_KEY)

    assert await read_state(database_at_version_one) == before
    assert before.key_version == FIRST_VERSION


async def test_rotation_with_the_opt_in_moves_to_the_next_version(
    database_at_version_one: Database,
) -> None:
    vault = await start(database_at_version_one, SECOND_KEY, FIRST_KEY, allow_rotation=True)

    assert vault.current_version == SECOND_VERSION
    assert (await read_state(database_at_version_one)).key_version == SECOND_VERSION


def encoded(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def settings_with_previous_key(previous_file: Path, source: str) -> Settings:
    supplied = {
        PREVIOUS_SECRET_KEY_VAR: encoded(FIRST_KEY),
        PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_file),
    }
    return load_settings({SECRET_KEY_VAR: encoded(SECOND_KEY), source: supplied[source]})


async def establish_from(settings: Settings, database: Database, *, allow_rotation: bool) -> Vault:
    return await establish_vault(
        database,
        MasterKeys.from_settings(settings),
        allow_rotation=allow_rotation,
        key_description=settings.key_description,
        previous_key_description=settings.previous_key_description,
    )


@pytest.fixture
def previous_file(tmp_path: Path) -> Path:
    path = tmp_path / "previous.key"
    path.write_text(encoded(FIRST_KEY), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "source", [PREVIOUS_SECRET_KEY_VAR, PREVIOUS_SECRET_KEY_FILE_VAR], ids=["variable", "file"]
)
async def test_a_previous_key_from_either_source_rotates_and_reads_the_old_value(
    database_at_version_one: Database, previous_file: Path, source: str
) -> None:
    settings = settings_with_previous_key(previous_file, source)

    vault = await establish_from(settings, database_at_version_one, allow_rotation=True)

    stored = scalar(database_at_version_one, "SELECT secret_ciphertext FROM registry_credential")
    assert vault.current_version == SECOND_VERSION
    assert vault.open(stored, SECRET_LOCATION) == PLAINTEXT


@pytest.mark.parametrize(
    ("source", "described"),
    [
        (PREVIOUS_SECRET_KEY_VAR, f"the configured {PREVIOUS_SECRET_KEY_VAR}"),
        (PREVIOUS_SECRET_KEY_FILE_VAR, f"({PREVIOUS_SECRET_KEY_FILE_VAR})"),
    ],
    ids=["variable", "file"],
)
async def test_a_rotation_refusal_names_the_source_of_the_previous_key(
    database_at_version_one: Database, previous_file: Path, source: str, described: str
) -> None:
    settings = settings_with_previous_key(previous_file, source)

    with pytest.raises(KeyMismatchError) as raised:
        await establish_from(settings, database_at_version_one, allow_rotation=False)

    assert f"The previous key, from {settings.previous_key_description}," in str(raised.value)
    assert described in str(raised.value)


@pytest.mark.parametrize(
    "stranded", ["", "v0:AAAA:BBBB", "not-a-token"], ids=["placeholder", "version zero", "garbage"]
)
async def test_rotation_blocked_by_a_value_that_is_not_ciphertext_names_the_table_and_row(
    database: Database, stranded: str
) -> None:
    vault = await start(database, FIRST_KEY)
    row_id = await add_credential(database, vault)
    execute_sql(
        database,
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (stranded, row_id),
    )

    with pytest.raises(KeyMismatchError) as raised:
        await start(database, SECOND_KEY, FIRST_KEY, allow_rotation=True)

    assert f"{SECRET.table} row {row_id}" in str(raised.value)
    assert "version 0" not in str(raised.value)


async def test_start_with_a_malformed_stored_instance_id_refuses(
    database_at_version_one: Database,
) -> None:
    execute_sql(
        database_at_version_one,
        "UPDATE setting SET value = 'nope' WHERE key = ?",
        (INSTANCE_ID_KEY,),
    )

    with pytest.raises(KeyMismatchError, match="instance id"):
        await start(database_at_version_one, FIRST_KEY)


async def test_instance_id_differs_between_databases_started_independently(
    database: Database, tmp_path: Path, migrated_template: Path
) -> None:
    other_path = tmp_path / "other.db"
    shutil.copyfile(migrated_template, other_path)
    await start(database, FIRST_KEY)
    await start(Database(other_path), FIRST_KEY)

    one = await read_state(database)
    two = await read_state(Database(other_path))

    assert one.instance_id != two.instance_id
