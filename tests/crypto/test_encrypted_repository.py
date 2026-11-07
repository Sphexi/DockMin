"""Tests for the secret-column repository base in app/db/repositories/encrypted.py."""

import pytest

from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.keys import Keyring
from app.crypto.registry import ENVIRONMENT_TLS_CLIENT_KEY, REGISTRY_CREDENTIAL_SECRET
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.encrypted import SecretWriteError
from tests.crypto.helpers import (
    CREDENTIAL_NAME,
    FIRST_KEY,
    FIRST_ROW,
    FIRST_VERSION,
    INSTANCE_ONE,
    OTHER_CREDENTIAL_NAME,
    OTHER_PLAINTEXT,
    PLAINTEXT,
    SECOND_ROW,
    CredentialRepository,
    count_rows,
    seed_row,
    stored_value,
)

TABLE = REGISTRY_CREDENTIAL_SECRET.table
SECRET = REGISTRY_CREDENTIAL_SECRET
NULLABLE = ENVIRONMENT_TLS_CLIENT_KEY


class SealFailure(Exception):
    """Raised by a vault that cannot seal, to fail an insert between its two statements."""


class FailingVault(Vault):
    """A vault whose seal always fails."""

    def seal(self, plaintext: str, location: SecretLocation) -> str:
        """Fail instead of sealing."""
        raise SealFailure


@pytest.fixture
def failing(session: Session) -> CredentialRepository:
    return CredentialRepository(
        session, FailingVault(Keyring(FIRST_VERSION, {FIRST_VERSION: FIRST_KEY}), INSTANCE_ONE)
    )


async def test_insert_outside_a_transaction_stores_a_sealed_value_for_the_new_row(
    session: Session, credentials: CredentialRepository
) -> None:
    row_id = await credentials.add(CREDENTIAL_NAME, PLAINTEXT)

    stored = await stored_value(session, SECRET, row_id)
    assert stored is not None
    assert stored.startswith(f"v{FIRST_VERSION}:")
    assert await credentials.reveal(SECRET, row_id) == PLAINTEXT


async def test_insert_inside_a_callers_transaction_commits_a_sealed_value(
    session: Session, credentials: CredentialRepository
) -> None:
    async with session.transaction():
        row_id = await credentials.add(CREDENTIAL_NAME, PLAINTEXT)

    stored = await stored_value(session, SECRET, row_id)
    assert stored is not None
    assert stored.startswith(f"v{FIRST_VERSION}:")
    assert await credentials.reveal(SECRET, row_id) == PLAINTEXT


async def test_failed_seal_inside_a_transaction_the_caller_commits_leaves_no_row(
    session: Session, failing: CredentialRepository
) -> None:
    async with session.transaction():
        with pytest.raises(SealFailure):
            await failing.add(CREDENTIAL_NAME, PLAINTEXT)

    assert await count_rows(session, TABLE) == 0


async def test_failed_seal_outside_a_transaction_leaves_no_row(
    session: Session, failing: CredentialRepository
) -> None:
    with pytest.raises(SealFailure):
        await failing.add(CREDENTIAL_NAME, PLAINTEXT)

    assert await count_rows(session, TABLE) == 0


async def test_failed_seal_keeps_the_callers_earlier_writes(
    session: Session, credentials: CredentialRepository, failing: CredentialRepository
) -> None:
    async with session.transaction():
        kept = await credentials.add(CREDENTIAL_NAME, PLAINTEXT)
        with pytest.raises(SealFailure):
            await failing.add(OTHER_CREDENTIAL_NAME, OTHER_PLAINTEXT)

    assert await count_rows(session, TABLE) == 1
    assert await credentials.reveal(SECRET, kept) == PLAINTEXT


async def test_ciphertext_copied_into_another_row_fails_to_decrypt_there(
    session: Session, credentials: CredentialRepository
) -> None:
    await credentials.add(CREDENTIAL_NAME, PLAINTEXT)
    await credentials.add(OTHER_CREDENTIAL_NAME, OTHER_PLAINTEXT)

    await session.execute(
        f"UPDATE {TABLE} SET {SECRET.column} = "
        f"(SELECT {SECRET.column} FROM {TABLE} WHERE id = ?) WHERE id = ?",
        (FIRST_ROW, SECOND_ROW),
    )

    with pytest.raises(DecryptionError):
        await credentials.reveal(SECRET, SECOND_ROW)
    assert await credentials.reveal(SECRET, FIRST_ROW) == PLAINTEXT


async def test_sealing_none_clears_a_nullable_secret_and_reads_back_as_none(
    session: Session, credentials: CredentialRepository
) -> None:
    row_id = await seed_row(session, NULLABLE.table)
    await credentials.seal(NULLABLE, row_id, PLAINTEXT)

    await credentials.seal(NULLABLE, row_id, None)

    assert await stored_value(session, NULLABLE, row_id) is None
    assert await credentials.reveal(NULLABLE, row_id) is None


@pytest.mark.parametrize(
    "sql",
    [
        f"UPDATE {TABLE} SET {SECRET.column} = 'plain'",
        f"update {TABLE} set {SECRET.column.upper()} = 'plain'",
    ],
)
async def test_free_form_sql_naming_a_secret_column_is_refused(
    credentials: CredentialRepository, sql: str
) -> None:
    with pytest.raises(SecretWriteError):
        await credentials.raw_execute(sql)
