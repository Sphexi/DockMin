"""Log scrubbing around key material and stored secrets."""

import json

import pytest

from app.crypto.keys import MasterKeys
from app.crypto.registry import REGISTRY_CREDENTIAL_SECRET
from app.crypto.startup import CANARY_PLAINTEXT, establish_vault
from app.db.engine import Database
from app.logging import DEFAULT_SCRUBBER, REDACTED, Scrubber
from tests.crypto.helpers import (
    FIRST_KEY,
    OTHER_CREDENTIAL_NAME,
    PLUS_AND_SLASH_KEY,
    CredentialRepository,
    encoded_forms,
    format_record,
)

COMMON_WORD = "INFO"

KEY_FORMS = [
    pytest.param(form, id=f"{name}-{kind}")
    for name, key in (("current", PLUS_AND_SLASH_KEY), ("previous", FIRST_KEY))
    for kind, form in zip(("base64", "urlsafe", "hex"), encoded_forms(key), strict=True)
]


@pytest.fixture
async def scrubber(database: Database) -> Scrubber:
    """A scrubber that has been through a startup with both keys configured."""
    private = Scrubber()
    await establish_vault(database, MasterKeys(PLUS_AND_SLASH_KEY, FIRST_KEY), private)
    return private


@pytest.mark.parametrize("form", KEY_FORMS)
def test_key_material_that_reaches_a_log_line_is_redacted_in_the_emitted_text(
    scrubber: Scrubber, form: str
) -> None:
    output = format_record(scrubber, f"unexpected value {form}")

    assert form not in output
    assert REDACTED in output


async def test_stored_secret_equal_to_a_common_word_does_not_redact_that_word_elsewhere(
    database: Database, scrubber: Scrubber
) -> None:
    vault = await establish_vault(database, MasterKeys(PLUS_AND_SLASH_KEY, FIRST_KEY), scrubber)
    async with database.session() as session:
        repo = CredentialRepository(session, vault)
        row_id = await repo.add(OTHER_CREDENTIAL_NAME, COMMON_WORD)
        await repo.reveal(REGISTRY_CREDENTIAL_SECRET, row_id)
    line = f"{COMMON_WORD} request handled"

    output = format_record(scrubber, line)

    assert json.loads(output)["message"] == line
    assert DEFAULT_SCRUBBER.scrub_text(line) == line


def test_canary_plaintext_is_not_treated_as_a_secret(scrubber: Scrubber) -> None:
    line = f"verified {CANARY_PLAINTEXT}"

    output = format_record(scrubber, line)

    assert json.loads(output)["message"] == line
