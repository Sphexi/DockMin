"""The master keys reach no log record, error message, exception body, repr or HTTP response."""

import logging
import shutil
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import (
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_VAR,
    ConfigError,
    Settings,
    load_settings,
)
from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.keys import Keyring, MasterKeys
from app.crypto.startup import KeyMismatchError
from app.crypto.vault import Vault
from app.db.engine import Database
from app.logging import DEFAULT_SCRUBBER
from app.main import on_startup
from app.selfcheck import StartupCheckFailed
from tests.crypto.helpers import (
    FIRST_KEY,
    FIRST_VERSION,
    INSTANCE_ONE,
    PLAINTEXT,
    SECOND_KEY,
    SECOND_VERSION,
    THIRD_KEY,
    emitted,
    leaked,
    start,
    vault_with,
)
from tests.support import KEY_MATERIAL, VALID_KEY

LOADED_EVENT = "encryption_key_loaded"
FAILED_EVENT = "startup_check_failed"
LOCATION = SecretLocation("registry_credential", "secret_ciphertext", 1)
OTHER_LOCATION = SecretLocation("registry_credential", "secret_ciphertext", 2)
SHORT_KEY_BASE64 = "AAAAAAAAAAAAAAAAAAAAAA=="
NOT_BASE64 = "###-not-base64-###"

KEY_HOLDERS = {
    "master keys": MasterKeys(FIRST_KEY, SECOND_KEY),
    "keyring": Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY, SECOND_VERSION: SECOND_KEY}),
    "vault": Vault(
        Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY, SECOND_VERSION: SECOND_KEY}),
        INSTANCE_ONE,
    ),
}

ENDPOINTS = [
    ("/api/health", 200),
    ("/api/ready", 200),
    ("/api/decrypt-failure", 500),
    ("/missing", 404),
]


async def _decrypt_failure() -> None:
    sealer = vault_with(KEY_MATERIAL)
    sealer.open(sealer.seal(PLAINTEXT, LOCATION), OTHER_LOCATION)


@pytest.fixture
def app(private_app: FastAPI) -> FastAPI:
    app = private_app
    app.add_api_route("/api/decrypt-failure", _decrypt_failure)
    return app


@pytest.fixture
def default_scrubber_restored() -> Iterator[None]:
    held = DEFAULT_SCRUBBER._secret_values
    yield
    DEFAULT_SCRUBBER._secret_values = held


async def test_first_start_and_rotation_log_fingerprints_and_no_key_material(
    database_at_version_one: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await start(database_at_version_one, SECOND_KEY, FIRST_KEY, allow_rotation=True)

    text = emitted(caplog.records)
    assert LOADED_EVENT in text
    assert leaked(text, FIRST_KEY, SECOND_KEY) == []


async def test_ordinary_restart_logs_fingerprints_and_no_key_material(
    database_at_version_one: Database, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    await start(database_at_version_one, FIRST_KEY)

    text = emitted(caplog.records)
    assert LOADED_EVENT in text
    assert leaked(text, FIRST_KEY) == []


@pytest.mark.usefixtures("default_scrubber_restored")
async def test_launch_with_the_wrong_key_logs_the_failure_and_raises_without_key_material(
    app: FastAPI,
    settings: Settings,
    migrated_template: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    shutil.copyfile(migrated_template, settings.db_path)
    await start(Database(settings.db_path), FIRST_KEY)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StartupCheckFailed) as raised:
        await on_startup(app)

    text = emitted(caplog.records) + "".join(traceback.format_exception(raised.value))
    assert FAILED_EVENT in text
    assert raised.value.check == "encryption_key"
    assert leaked(text, KEY_MATERIAL, FIRST_KEY) == []


async def test_key_mismatch_message_and_traceback_contain_no_key_material(
    database_at_version_one: Database,
) -> None:
    with pytest.raises(KeyMismatchError) as raised:
        await start(database_at_version_one, SECOND_KEY, THIRD_KEY)

    text = str(raised.value) + "".join(traceback.format_exception(raised.value))
    assert leaked(text, FIRST_KEY, SECOND_KEY, THIRD_KEY) == []


def test_decryption_error_message_and_traceback_contain_no_key_or_plaintext() -> None:
    sealer = vault_with(FIRST_KEY)
    token = sealer.seal(PLAINTEXT, LOCATION)

    with pytest.raises(DecryptionError) as raised:
        vault_with(SECOND_KEY).open(token, LOCATION)

    text = str(raised.value) + "".join(traceback.format_exception(raised.value))
    assert leaked(text, FIRST_KEY, SECOND_KEY) == []
    assert PLAINTEXT not in text
    assert token not in text


@pytest.mark.parametrize("rejected", [SHORT_KEY_BASE64, NOT_BASE64])
def test_rejected_previous_key_is_named_by_variable_and_never_echoed(rejected: str) -> None:
    with pytest.raises(ConfigError) as raised:
        load_settings({SECRET_KEY_VAR: VALID_KEY, PREVIOUS_SECRET_KEY_VAR: rejected})

    message = str(raised.value)
    assert PREVIOUS_SECRET_KEY_VAR in message
    assert rejected not in message
    assert VALID_KEY not in message


@pytest.mark.parametrize("holder", KEY_HOLDERS.values(), ids=KEY_HOLDERS.keys())
def test_repr_and_str_of_an_object_holding_keys_contain_no_key_material(holder: object) -> None:
    text = repr(holder) + str(holder)

    assert leaked(text, FIRST_KEY, SECOND_KEY) == []


@pytest.mark.parametrize(("path", "status"), ENDPOINTS)
async def test_http_response_and_emitted_logs_contain_no_key_material(
    admin_client: AsyncClient, caplog: pytest.LogCaptureFixture, path: str, status: int
) -> None:
    caplog.set_level(logging.DEBUG)

    response = await admin_client.get(path)

    sent = response.text + str(dict(response.headers))
    assert response.status_code == status
    assert leaked(sent, KEY_MATERIAL) == []
    assert leaked(emitted(caplog.records), KEY_MATERIAL) == []
    assert PLAINTEXT not in sent
