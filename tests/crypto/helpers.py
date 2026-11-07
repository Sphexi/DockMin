"""Constants and builders shared by the credential encryption tests."""

import base64
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

import pytest

from app.config import KEY_BYTES
from app.crypto.keys import Keyring, MasterKeys
from app.crypto.registry import (
    ENVIRONMENT_TLS_CLIENT_KEY,
    NOTIFICATION_CHANNEL_SECRETS,
    OBJECT_STORAGE_SECRET_ACCESS_KEY,
    REGISTRY_CREDENTIAL_SECRET,
    SECRET_FIELDS,
    STACK_FILE_CONTENT,
    STACK_VARIABLE_VALUE,
    SecretField,
)
from app.crypto.startup import establish_vault
from app.crypto.vault import Vault
from app.db.engine import Database, Session, WriteResult
from app.db.repositories.crypto_state import CryptoState, CryptoStateRepository
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.logging import JsonFormatter, Scrubber

FIRST_KEY = bytes([1]) * KEY_BYTES
SECOND_KEY = bytes([2]) * KEY_BYTES
THIRD_KEY = bytes([3]) * KEY_BYTES
PLUS_AND_SLASH_KEY = bytes([0xFB]) * KEY_BYTES

FIRST_VERSION = 1
SECOND_VERSION = 2

INSTANCE_ONE = "a" * 32
INSTANCE_TWO = "b" * 32

PLAINTEXT = "plaintext-under-test"
OTHER_PLAINTEXT = "another-plaintext-under-test"
CREDENTIAL_NAME = "credential-one"
OTHER_CREDENTIAL_NAME = "credential-two"

FIRST_ROW = 1
SECOND_ROW = 2

STAMP = "2026-01-02T03:04:05Z"
REGISTRY_URL = "registry.example.test"
REGISTRY_USERNAME = "service"
SOCKET_PATH = "/var/run/docker.sock"

CRYPTO_STATE_PATTERN = "crypto.%"
CRYPTO_STATE_ROWS = 3


def encoded_forms(key: bytes) -> tuple[str, ...]:
    """Return the text encodings a key is likely to be logged in."""
    return (
        base64.b64encode(key).decode("ascii"),
        base64.urlsafe_b64encode(key).decode("ascii"),
        key.hex(),
    )


def key_texts(key: bytes) -> tuple[str, ...]:
    """Return every text form a key could leak in."""
    return (*encoded_forms(key), repr(key))


def leaked(text: str, *keys: bytes) -> list[str]:
    """Return every form of any key that appears in text."""
    return [form for key in keys for form in key_texts(key) if form in text]


def emitted(records: Iterable[logging.LogRecord]) -> str:
    """Render records through an unscrubbed JSON formatter, as the text a log sink receives."""
    formatter = JsonFormatter(Scrubber())
    return "\n".join(formatter.format(record) for record in records)


def format_record(scrubber: Scrubber, message: str) -> str:
    """Render one INFO record through a formatter that uses scrubber."""
    record = logging.LogRecord("tests", logging.INFO, __file__, 0, message, (), None)
    return JsonFormatter(scrubber).format(record)


def tamper(token: str) -> str:
    """Return token with its first ciphertext character changed."""
    version, nonce, body = token.split(":")
    replacement = "B" if body[0] == "A" else "A"
    return f"{version}:{nonce}:{replacement}{body[1:]}"


def vault_with(key: bytes, instance_id: str = INSTANCE_ONE, version: int = FIRST_VERSION) -> Vault:
    """Build a vault holding one key at one version."""
    return Vault(Keyring(version, {version: key}), instance_id)


class CredentialRepository(SecretRepository[None]):
    """A repository over every registered secret column, with public entry points for tests."""

    _secret_columns = tuple(EncryptedStr(field) for field in SECRET_FIELDS)

    def _map_row(self, row: sqlite3.Row) -> None:
        return None

    async def add(self, name: str, plaintext: str) -> int:
        """Insert a static registry credential and seal its secret."""
        return await self._insert_with_secrets(
            "registry_credential",
            {
                "name": name,
                "provider": "custom",
                "url": REGISTRY_URL,
                "authentication_enabled": 1,
                "username": REGISTRY_USERNAME,
                "secret_kind": "static",
                "created_at": STAMP,
                "updated_at": STAMP,
            },
            {EncryptedStr(REGISTRY_CREDENTIAL_SECRET): plaintext},
        )

    async def seal(self, field: SecretField, row_id: int, plaintext: str | None) -> bool:
        """Seal and store one secret in an existing row."""
        return await self._set_secret(EncryptedStr(field), row_id, plaintext)

    async def reveal(self, field: SecretField, row_id: int) -> str | None:
        """Read and decrypt one secret."""
        row = await self._session.fetch_one(
            f"SELECT id, {field.column} FROM {field.table} WHERE id = ?", (row_id,)
        )
        assert row is not None
        return self._reveal(EncryptedStr(field), row)

    async def raw_execute(self, sql: str) -> WriteResult:
        """Run free-form SQL through the repository's guard."""
        return await self._execute(sql)


def _row_id(result: WriteResult) -> int:
    assert result.last_row_id is not None
    return result.last_row_id


async def _seed_registry_credential(session: Session) -> int:
    result = await session.execute(
        "INSERT INTO registry_credential (name, provider, url, authentication_enabled, "
        "username, secret_kind, secret_ciphertext, derived_secret_ciphertext, "
        "derived_secret_expires_at, created_at, updated_at) "
        "VALUES (?, 'ecr', ?, 1, ?, 'derived', '', '', ?, ?, ?)",
        (CREDENTIAL_NAME, REGISTRY_URL, REGISTRY_USERNAME, STAMP, STAMP, STAMP),
    )
    return _row_id(result)


async def _seed_environment_tls_material(session: Session) -> int:
    environment = await session.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES (?, 'standalone', 'unix_socket', ?, ?, ?)",
        (CREDENTIAL_NAME, SOCKET_PATH, STAMP, STAMP),
    )
    material = await session.execute(
        "INSERT INTO environment_tls_material (environment_id, created_at, updated_at) "
        "VALUES (?, ?, ?)",
        (_row_id(environment), STAMP, STAMP),
    )
    return _row_id(material)


async def _seed_object_storage_target(session: Session) -> int:
    result = await session.execute(
        "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
        "secret_access_key_ciphertext, use_path_style, backup_prefix, activity_log_prefix, "
        "created_at, updated_at) VALUES (?, ?, 'r', 'b', 'k', '', 0, 'backups/', 'logs/', ?, ?)",
        (CREDENTIAL_NAME, REGISTRY_URL, STAMP, STAMP),
    )
    return _row_id(result)


async def _seed_notification_channel(session: Session) -> int:
    result = await session.execute(
        "INSERT INTO notification_channel (name, kind, settings, secrets_ciphertext, created_at, "
        "updated_at) VALUES (?, 'webhook', '{}', '', ?, ?)",
        (CREDENTIAL_NAME, STAMP, STAMP),
    )
    return _row_id(result)


STACK_DIRECTORY = "c" * 32
STACK_NAME = "stack-one"
FILE_DIGEST = "d" * 64


async def _seed_stack(session: Session) -> int:
    existing = await session.fetch_one("SELECT id FROM stack WHERE name = ?", (STACK_NAME,))
    if existing is not None:
        return int(existing["id"])
    environment = await session.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, created_at, "
        "updated_at) VALUES (?, 'standalone', 'unix_socket', ?, ?, ?)",
        (CREDENTIAL_NAME, SOCKET_PATH, STAMP, STAMP),
    )
    stack = await session.execute(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (?, ?, 'compose', 'created', ?, 'services: {}', ?, ?, ?)",
        (_row_id(environment), STACK_NAME, STACK_DIRECTORY, FILE_DIGEST, STAMP, STAMP),
    )
    return _row_id(stack)


async def _seed_stack_variable(session: Session) -> int:
    variable = await session.execute(
        "INSERT INTO stack_variable (stack_id, name, is_secret, value_ciphertext, created_at, "
        "updated_at) VALUES (?, 'TOKEN', 1, '', ?, ?)",
        (await _seed_stack(session), STAMP, STAMP),
    )
    return _row_id(variable)


async def _seed_stack_file(session: Session) -> int:
    file = await session.execute(
        "INSERT INTO stack_file (stack_id, name, is_secret, content_ciphertext, size_bytes, "
        "value_token, created_at, updated_at) VALUES (?, 'secret.env', 1, '', 1, ?, ?, ?)",
        (await _seed_stack(session), "abcdefghij12", STAMP, STAMP),
    )
    return _row_id(file)


SEED_RECIPES: dict[str, Callable[[Session], Awaitable[int]]] = {
    REGISTRY_CREDENTIAL_SECRET.table: _seed_registry_credential,
    ENVIRONMENT_TLS_CLIENT_KEY.table: _seed_environment_tls_material,
    OBJECT_STORAGE_SECRET_ACCESS_KEY.table: _seed_object_storage_target,
    STACK_VARIABLE_VALUE.table: _seed_stack_variable,
    STACK_FILE_CONTENT.table: _seed_stack_file,
    NOTIFICATION_CHANNEL_SECRETS.table: _seed_notification_channel,
}


async def seed_row(session: Session, table: str) -> int:
    """Insert the smallest valid row of a registered secret table and return its id."""
    recipe = SEED_RECIPES.get(table)
    if recipe is None:
        pytest.fail(f"a secret field was added to {table!r}; add a seed recipe in helpers.py")
    return await recipe(session)


async def stored_value(session: Session, field: SecretField, row_id: int) -> str | None:
    """Return the raw column value, exactly as stored."""
    row = await session.fetch_one(
        f"SELECT {field.column} AS stored FROM {field.table} WHERE id = ?", (row_id,)
    )
    assert row is not None
    value: str | None = row["stored"]
    return value


async def count_rows(session: Session, table: str) -> int:
    row = await session.fetch_one(f"SELECT COUNT(*) AS total FROM {table}")
    assert row is not None
    return int(row["total"])


async def start(
    database: Database,
    current: bytes,
    previous: bytes | None = None,
    *,
    allow_rotation: bool = False,
    key_description: str | None = None,
) -> Vault:
    """Run startup verification with a private scrubber."""
    options = {} if key_description is None else {"key_description": key_description}
    return await establish_vault(
        database,
        MasterKeys(current, previous),
        Scrubber(),
        allow_rotation=allow_rotation,
        **options,
    )


async def add_credential(
    database: Database, vault: Vault, plaintext: str = PLAINTEXT, name: str = CREDENTIAL_NAME
) -> int:
    """Insert a sealed registry credential on its own connection and return its id."""
    async with database.session() as session:
        return await CredentialRepository(session, vault).add(name, plaintext)


async def read_state(database: Database) -> CryptoState:
    """Return the stored crypto state, failing the test when it is absent."""
    async with database.session() as session:
        state = await CryptoStateRepository(session).get()
    assert state is not None
    return state


def scalar(database: Database, sql: str, params: Sequence[Any] = ()) -> Any:
    """Return the first column of the first row, on a throwaway connection."""
    connection = database.connect()
    try:
        return connection.execute(sql, params).fetchone()[0]
    finally:
        connection.close()


def execute_sql(database: Database, sql: str, params: Sequence[Any] = ()) -> None:
    """Run one statement on a throwaway connection, committed immediately."""
    connection = database.connect()
    try:
        connection.execute(sql, params)
    finally:
        connection.close()


class TracingDatabase(Database):
    """A database whose connections record every statement they run."""

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.statements: list[str] = []

    def connect(self) -> sqlite3.Connection:
        """Open a connection that appends each statement to statements."""
        connection = super().connect()
        connection.set_trace_callback(self.statements.append)
        return connection

    def statements_naming_a_secret_column(self) -> list[str]:
        """Return the recorded statements that mention any registered secret column."""
        return [
            statement
            for statement in self.statements
            if any(field.column in statement for field in SECRET_FIELDS)
        ]
