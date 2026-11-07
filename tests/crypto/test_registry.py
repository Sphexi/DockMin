"""Tests for app/crypto/registry.py and the secret columns it declares."""

import pytest

from app.crypto.registry import SECRET_FIELDS, SecretField
from app.db.engine import Database, Session
from app.db.repositories.crypto_state import CryptoStateRepository
from app.db.repositories.encrypted import EncryptedStr
from tests.crypto.helpers import (
    FIRST_VERSION,
    PLAINTEXT,
    CredentialRepository,
    seed_row,
    stored_value,
)

CIPHERTEXT_COLUMN_SUFFIX = "_ciphertext"
SCHEMA_CIPHERTEXT_COLUMNS = (
    "SELECT m.name AS table_name, p.name AS column_name FROM sqlite_master AS m "
    "JOIN pragma_table_info(m.name) AS p WHERE m.type = 'table' "
    "AND p.name LIKE '%' || ? ESCAPE '\\'"
)


@pytest.mark.parametrize("field", SECRET_FIELDS, ids=str)
async def test_registered_secret_field_round_trips_and_is_stored_as_ciphertext(
    session: Session, credentials: CredentialRepository, field: SecretField
) -> None:
    row_id = await seed_row(session, field.table)

    await credentials.seal(field, row_id, PLAINTEXT)

    stored = await stored_value(session, field, row_id)
    assert stored is not None
    assert stored != PLAINTEXT
    assert PLAINTEXT not in stored
    assert stored.startswith(f"v{FIRST_VERSION}:")
    assert await credentials.reveal(field, row_id) == PLAINTEXT


@pytest.mark.parametrize("field", SECRET_FIELDS, ids=str)
async def test_version_count_includes_a_value_in_every_registered_field(
    session: Session, credentials: CredentialRepository, field: SecretField
) -> None:
    row_id = await seed_row(session, field.table)
    await credentials.seal(field, row_id, PLAINTEXT)

    counts = await CryptoStateRepository(session).count_by_version()

    assert counts == {FIRST_VERSION: 1}


def test_every_ciphertext_column_in_the_schema_is_in_the_registry(database: Database) -> None:
    connection = database.connect()
    try:
        rows = connection.execute(SCHEMA_CIPHERTEXT_COLUMNS, (CIPHERTEXT_COLUMN_SUFFIX,)).fetchall()
    finally:
        connection.close()

    in_schema = {(row["table_name"], row["column_name"]) for row in rows}

    assert in_schema == {(field.table, field.column) for field in SECRET_FIELDS}


def test_registry_lists_each_field_once() -> None:
    assert len(set(SECRET_FIELDS)) == len(SECRET_FIELDS)


def test_encrypted_str_refuses_a_column_that_is_not_registered() -> None:
    with pytest.raises(ValueError, match="registry"):
        EncryptedStr(SecretField("user", "password_hash"))


@pytest.mark.parametrize(
    ("table", "column"), [("User", "secret"), ("user", "secret;"), ("", "secret"), ("user", "")]
)
def test_secret_field_rejects_names_that_are_not_plain_identifiers(table: str, column: str) -> None:
    with pytest.raises(ValueError, match="identifiers"):
        SecretField(table, column)
