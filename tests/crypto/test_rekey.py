"""Tests for app/db/repositories/rekey.py."""

import pytest

from app.crypto.envelope import SecretLocation
from app.crypto.keys import Keyring
from app.crypto.registry import REGISTRY_CREDENTIAL_SECRET
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.rekey import RekeyFailure, RekeyPass
from tests.crypto.helpers import (
    FIRST_KEY,
    FIRST_ROW,
    FIRST_VERSION,
    INSTANCE_ONE,
    OTHER_CREDENTIAL_NAME,
    OTHER_PLAINTEXT,
    PLAINTEXT,
    SECOND_KEY,
    SECOND_ROW,
    SECOND_VERSION,
    CredentialRepository,
    TracingDatabase,
    stored_value,
    tamper,
)

SECRET = REGISTRY_CREDENTIAL_SECRET
ONE_ROW_PER_BATCH = 1
THIRD_ROW = 3
FOURTH_ROW = 4
THIRD_PLAINTEXT = "third-plaintext-under-test"
THIRD_CREDENTIAL_NAME = "credential-three"
ALL_ROWS = (FIRST_ROW, SECOND_ROW, THIRD_ROW)
PLAINTEXTS = {FIRST_ROW: PLAINTEXT, SECOND_ROW: OTHER_PLAINTEXT, THIRD_ROW: THIRD_PLAINTEXT}


@pytest.fixture
def rotated_vault() -> Vault:
    """A vault at version 2 that can still read version 1."""
    keyring = Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY, SECOND_VERSION: SECOND_KEY})
    return Vault(keyring, INSTANCE_ONE)


@pytest.fixture
async def three_values_at_version_one(credentials: CredentialRepository) -> None:
    await credentials.add("credential-one", PLAINTEXT)
    await credentials.add(OTHER_CREDENTIAL_NAME, OTHER_PLAINTEXT)
    await credentials.add(THIRD_CREDENTIAL_NAME, THIRD_PLAINTEXT)


@pytest.mark.usefixtures("three_values_at_version_one")
async def test_interrupted_rekey_leaves_every_value_readable_at_its_own_version(
    session: Session, rotated_vault: Vault
) -> None:
    rekey = RekeyPass(session, rotated_vault, batch_size=ONE_ROW_PER_BATCH)
    progress = rekey.run()

    first_batch = await anext(progress)
    await progress.aclose()

    reader = CredentialRepository(session, rotated_vault)
    assert first_batch.remaining_by_version == {FIRST_VERSION: 2, SECOND_VERSION: 1}
    assert not first_batch.complete
    for row_id in ALL_ROWS:
        assert await reader.reveal(SECRET, row_id) == PLAINTEXTS[row_id]


@pytest.mark.usefixtures("three_values_at_version_one")
async def test_resumed_rekey_finishes_the_values_the_interrupted_pass_left(
    session: Session, rotated_vault: Vault
) -> None:
    interrupted = RekeyPass(session, rotated_vault, batch_size=ONE_ROW_PER_BATCH).run()
    await anext(interrupted)
    await interrupted.aclose()

    updates = [step async for step in RekeyPass(session, rotated_vault).run()]

    reader = CredentialRepository(session, rotated_vault)
    assert updates[-1].complete
    assert updates[-1].remaining_by_version == {SECOND_VERSION: len(ALL_ROWS)}
    assert updates[-1].rewritten_total == len(ALL_ROWS) - 1
    for row_id in ALL_ROWS:
        assert await reader.reveal(SECRET, row_id) == PLAINTEXTS[row_id]


@pytest.mark.usefixtures("three_values_at_version_one")
async def test_rekey_run_again_after_completion_rewrites_nothing(
    session: Session, rotated_vault: Vault
) -> None:
    [step async for step in RekeyPass(session, rotated_vault).run()]

    updates = [step async for step in RekeyPass(session, rotated_vault).run()]

    assert updates[-1].rewritten_total == 0


@pytest.mark.usefixtures("three_values_at_version_one")
async def test_rekey_reports_an_unreadable_value_by_location_and_still_rewrites_the_rest(
    session: Session, rotated_vault: Vault
) -> None:
    damaged = await stored_value(session, SECRET, SECOND_ROW)
    assert damaged is not None
    await session.execute(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (tamper(damaged), SECOND_ROW),
    )

    updates = [step async for step in RekeyPass(session, rotated_vault).run()]

    final = updates[-1]
    assert final.failures == (RekeyFailure(SECRET, SECOND_ROW),)
    assert final.remaining_by_version == {FIRST_VERSION: 1, SECOND_VERSION: 2}
    assert PLAINTEXT not in repr(final)


@pytest.mark.usefixtures("three_values_at_version_one")
async def test_rekeyed_value_authenticates_only_at_its_own_row(
    session: Session, rotated_vault: Vault
) -> None:
    [step async for step in RekeyPass(session, rotated_vault).run()]

    stored = await stored_value(session, SECRET, FIRST_ROW)

    assert stored is not None
    assert rotated_vault.open(stored, SecretLocation(SECRET.table, SECRET.column, FIRST_ROW)) == (
        PLAINTEXT
    )


@pytest.mark.parametrize("batch_size", [0, -1])
def test_rekey_pass_rejects_a_batch_size_below_one(
    session: Session, rotated_vault: Vault, batch_size: int
) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        RekeyPass(session, rotated_vault, batch_size=batch_size)


UNPARSEABLE_VALUE = "not-a-token"
WRITING_STATEMENTS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
CORRUPT_ROW = 2
UNPARSEABLE_ROW = 3


@pytest.fixture
async def current_values(session: Session, rotated_vault: Vault) -> None:
    """Three credentials sealed at the current version: one sound, one corrupt, one unparseable."""
    repository = CredentialRepository(session, rotated_vault)
    await repository.add("credential-one", PLAINTEXT)
    await repository.add(OTHER_CREDENTIAL_NAME, OTHER_PLAINTEXT)
    await repository.add(THIRD_CREDENTIAL_NAME, THIRD_PLAINTEXT)
    corrupt = await stored_value(session, SECRET, CORRUPT_ROW)
    assert corrupt is not None
    await session.execute(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (tamper(corrupt), CORRUPT_ROW),
    )
    await session.execute(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        (UNPARSEABLE_VALUE, UNPARSEABLE_ROW),
    )


async def stored_values(session: Session) -> list[str | None]:
    return [await stored_value(session, SECRET, row_id) for row_id in ALL_ROWS]


def writes_among(statements: list[str]) -> list[str]:
    return [
        statement for statement in statements if statement.split()[0].upper() in WRITING_STATEMENTS
    ]


@pytest.mark.usefixtures("current_values")
async def test_verify_reports_a_corrupt_and_an_unparseable_value_by_location_and_changes_nothing(
    session: Session, rotated_vault: Vault
) -> None:
    before = await stored_values(session)

    steps = [step async for step in RekeyPass(session, rotated_vault).verify()]

    final = steps[-1]
    assert final.complete
    assert set(final.failures) == {
        RekeyFailure(SECRET, CORRUPT_ROW, "corrupt"),
        RekeyFailure(SECRET, UNPARSEABLE_ROW, "unparseable"),
    }
    assert (final.rewritten_total, final.checked_total) == (0, 2)
    assert await stored_values(session) == before
    assert PLAINTEXT not in repr(final)


async def test_verify_leaves_a_value_below_the_current_version_where_it_is(
    session: Session, rotated_vault: Vault, credentials: CredentialRepository
) -> None:
    await credentials.add("credential-one", PLAINTEXT)
    before = await stored_value(session, SECRET, FIRST_ROW)

    steps = [step async for step in RekeyPass(session, rotated_vault).verify()]

    assert steps[-1].remaining_by_version == {FIRST_VERSION: 1}
    assert steps[-1].failures == ()
    assert await stored_value(session, SECRET, FIRST_ROW) == before


async def test_verify_issues_no_write_statement(
    traced_database: TracingDatabase, rotated_vault: Vault
) -> None:
    async with traced_database.session() as traced:
        await CredentialRepository(traced, rotated_vault).add("credential-one", PLAINTEXT)
        traced_database.statements.clear()

        [step async for step in RekeyPass(traced, rotated_vault).verify()]

    assert writes_among(traced_database.statements) == []
    assert traced_database.statements


@pytest.mark.usefixtures("current_values")
async def test_a_verifying_rekey_rewrites_nothing_it_cannot_open_and_still_reports_it(
    session: Session, rotated_vault: Vault, credentials: CredentialRepository
) -> None:
    await credentials.add("credential-four", PLAINTEXT)
    sound = await stored_value(session, SECRET, FOURTH_ROW)

    steps = [step async for step in RekeyPass(session, rotated_vault, verify_current=True).run()]

    final = steps[-1]
    reported = {(failure.row_id, failure.kind) for failure in final.failures}
    assert (CORRUPT_ROW, "corrupt") in reported
    assert (UNPARSEABLE_ROW, "unparseable") in reported
    assert final.rewritten_total == 1
    assert await stored_value(session, SECRET, FOURTH_ROW) != sound
    assert await stored_value(session, SECRET, UNPARSEABLE_ROW) == UNPARSEABLE_VALUE


@pytest.mark.usefixtures("current_values")
async def test_a_verifying_rekey_names_each_unopened_location_once(
    session: Session, rotated_vault: Vault
) -> None:
    steps = [step async for step in RekeyPass(session, rotated_vault, verify_current=True).run()]

    locations = [(failure.field, failure.row_id) for failure in steps[-1].failures]
    assert len(locations) == len(set(locations))
