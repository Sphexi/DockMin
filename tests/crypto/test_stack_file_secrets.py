"""A secret companion file is covered by the startup checks and the re-key pass."""

import pytest

from app.crypto.registry import STACK_FILE_CONTENT
from app.crypto.startup import KeyMismatchError
from app.db.engine import Database
from app.db.repositories.crypto_state import CANARY_KEY
from app.db.repositories.rekey import RekeyPass
from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_file import StackFileRepository
from tests.crypto.helpers import (
    FIRST_KEY,
    FIRST_VERSION,
    SECOND_KEY,
    SECOND_VERSION,
    CredentialRepository,
    execute_sql,
    scalar,
    seed_row,
    start,
)

FILE_NAME = "secret.env"
FILE_PLAINTEXT = "TOKEN=plain-file-secret\n"
FILE_CONTENT_SQL = "SELECT content_ciphertext FROM stack_file"


async def seal_a_secret_file(database: Database, vault_key: bytes) -> int:
    """Store one secret companion file sealed under the first key; return its stack id."""
    vault = await start(database, vault_key)
    async with database.session() as session:
        row_id = await seed_row(session, STACK_FILE_CONTENT.table)
        assert await CredentialRepository(session, vault).seal(
            STACK_FILE_CONTENT, row_id, FILE_PLAINTEXT
        )
        row = await session.fetch_one("SELECT stack_id FROM stack_file WHERE id = ?", (row_id,))
        assert row is not None
        return int(row["stack_id"])


async def test_a_stored_secret_file_makes_deleted_state_rows_a_refusal_to_start(
    database: Database,
) -> None:
    await seal_a_secret_file(database, FIRST_KEY)
    execute_sql(database, "DELETE FROM setting WHERE key LIKE ?", (CANARY_KEY,))

    with pytest.raises(KeyMismatchError, match="state rows are missing"):
        await start(database, SECOND_KEY)


async def test_a_secret_file_left_at_the_earlier_key_version_blocks_a_start_without_the_old_key(
    database: Database,
) -> None:
    await seal_a_secret_file(database, FIRST_KEY)
    await start(database, SECOND_KEY, FIRST_KEY, allow_rotation=True)

    with pytest.raises(KeyMismatchError) as raised:
        await start(database, SECOND_KEY)

    assert f"key version {FIRST_VERSION} (1 values)" in str(raised.value)


async def test_the_rekey_pass_moves_a_secret_file_to_the_new_key_and_it_stays_readable(
    database: Database,
) -> None:
    stack_id = await seal_a_secret_file(database, FIRST_KEY)
    rotated = await start(database, SECOND_KEY, FIRST_KEY, allow_rotation=True)
    before = scalar(database, FILE_CONTENT_SQL)

    async with database.session() as session:
        updates = [step async for step in RekeyPass(session, rotated).run()]
        stacks = StackRepository(session, rotated)
        revealed = await StackFileRepository(session, rotated, stacks=stacks).reveal_files(stack_id)

    after = scalar(database, FILE_CONTENT_SQL)
    assert updates[-1].complete
    assert revealed == {FILE_NAME: FILE_PLAINTEXT}
    assert before.startswith(f"v{FIRST_VERSION}:")
    assert after.startswith(f"v{SECOND_VERSION}:")
