"""EncryptedStr and the repository base that makes secret columns impossible to bypass."""

import re
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

from app.crypto.envelope import PLAIN_IDENTIFIER, SecretLocation
from app.crypto.registry import SECRET_FIELDS, SecretField
from app.crypto.vault import Vault
from app.db.engine import Params, Session, WriteResult
from app.db.repositories.clocked import Clock, ClockedRepository, utc_now

# Held in a secret column between an insert and the update that seals it, in one transaction;
# no reader sees it, and it is not a secret.
INSERT_PLACEHOLDER = ""


class SecretWriteError(Exception):
    """A statement tried to write a secret column without going through EncryptedStr."""


@dataclass(frozen=True, slots=True)
class EncryptedStr:
    """Declares a registered column as holding an encrypted string."""

    field: SecretField

    def __post_init__(self) -> None:
        if self.field not in SECRET_FIELDS:
            raise ValueError(f"{self.field} is not in the secret-field registry")

    @property
    def table(self) -> str:
        """Return the owning table."""
        return self.field.table

    @property
    def column(self) -> str:
        """Return the column name."""
        return self.field.column

    def location(self, row_id: int) -> SecretLocation:
        """Return the authenticated location of this column in one row."""
        return SecretLocation(self.field.table, self.field.column, row_id)

    def seal(self, vault: Vault, row_id: int, plaintext: str | None) -> str | None:
        """Encrypt for storage; None stays None so nullable columns keep their meaning."""
        return None if plaintext is None else vault.seal(plaintext, self.location(row_id))

    def open(self, vault: Vault, row_id: int, stored: str | None) -> str | None:
        """Decrypt a stored value; None stays None."""
        return None if stored is None else vault.open(stored, self.location(row_id))


def _identifier(name: str) -> str:
    if PLAIN_IDENTIFIER.fullmatch(name) is None:
        raise ValueError("column names must be plain lowercase identifiers")
    return name


class SecretRepository[T](ClockedRepository[T]):
    """Repository base for tables with secret columns, which it declares and guards.

    Subclasses list their columns in _secret_columns, write them only through
    _insert_with_secrets, _update_with_secrets and _set_secret, and read them only through
    _reveal.
    """

    _secret_columns: ClassVar[tuple[EncryptedStr, ...]] = ()

    def __init__(self, session: Session, vault: Vault, clock: Clock = utc_now) -> None:
        super().__init__(session, clock)
        self._vault = vault
        self._guards = tuple(
            re.compile(rf"\b{re.escape(column.column)}\b", re.IGNORECASE)
            for column in self._secret_columns
        )

    async def _execute(self, sql: str, params: Params = ()) -> WriteResult:
        # A secret column named in free-form SQL could be assigned a plaintext, so it is refused.
        if any(guard.search(sql) for guard in self._guards):
            raise SecretWriteError("secret columns are written only through EncryptedStr")
        return await super()._execute(sql, params)

    def _reveal(self, column: EncryptedStr, row: sqlite3.Row) -> str | None:
        return column.open(self._vault, int(row["id"]), row[column.column])

    async def _insert_with_secrets(
        self,
        table: str,
        values: Mapping[str, Any],
        secrets: Mapping[EncryptedStr, str | None],
    ) -> int:
        """Insert a row and seal its secrets against the id it receives; return the id."""
        declared = set(self._secret_columns)
        for column in secrets:
            if column not in declared or column.table != table:
                raise SecretWriteError(f"{column.field} is not a declared secret of {table}")
        plain_names = [_identifier(name) for name in values]
        if {column.column for column in secrets} & set(plain_names):
            raise SecretWriteError("a secret column cannot also be given as a plain value")
        names = [*plain_names, *(column.column for column in secrets)]
        bound = [
            *values.values(),
            *(None if text is None else INSERT_PLACEHOLDER for text in secrets.values()),
        ]
        placeholders = ", ".join("?" * len(names))
        sql = f"INSERT INTO {_identifier(table)} ({', '.join(names)}) VALUES ({placeholders})"
        async with self._atomic():
            result = await self._session.execute(sql, bound)
            row_id = result.last_row_id
            if row_id is None:
                raise SecretWriteError("an insert did not report a row id")
            for column, text in secrets.items():
                if text is not None and not await self._store(column, row_id, text):
                    raise SecretWriteError("an inserted row vanished before its secret was sealed")
        return row_id

    async def _update_with_secrets(
        self,
        table: str,
        row_id: int,
        values: Mapping[str, Any],
        secrets: Mapping[EncryptedStr, str | None],
        *,
        expect: Mapping[str, Any] | None = None,
    ) -> bool:
        """Update plain columns and seal secrets in one statement; False if no row matched.

        One statement, because a CHECK is evaluated per statement and columns that depend on one
        another must change together. `expect` adds a compare-and-swap predicate; a secret column
        in it is compared as stored, which is its ciphertext, and None matches NULL.
        """
        declared = set(self._secret_columns)
        for column in secrets:
            if column not in declared or column.table != table:
                raise SecretWriteError(f"{column.field} is not a declared secret of {table}")
        plain_names = [_identifier(name) for name in values]
        if {column.column for column in secrets} & set(plain_names):
            raise SecretWriteError("a secret column cannot also be given as a plain value")
        if not plain_names and not secrets:
            raise ValueError("an update needs at least one column")
        names = [*plain_names, *(column.column for column in secrets)]
        bound: list[Any] = [
            *values.values(),
            *(column.seal(self._vault, row_id, text) for column, text in secrets.items()),
        ]
        assignments = ", ".join(f"{name} = ?" for name in names)
        predicate = "id = ?"
        bound.append(row_id)
        for name, value in (expect or {}).items():
            predicate += f" AND {_identifier(name)} IS ?"
            bound.append(value)
        result = await self._session.execute(
            f"UPDATE {_identifier(table)} SET {assignments} WHERE {predicate}", bound
        )
        return result.rowcount > 0

    async def _set_secret(self, column: EncryptedStr, row_id: int, plaintext: str | None) -> bool:
        """Seal and store one secret; return False if the row does not exist."""
        if column not in self._secret_columns:
            raise SecretWriteError(f"{column.field} is not a declared secret of this repository")
        return await self._store(column, row_id, plaintext)

    async def _store(self, column: EncryptedStr, row_id: int, plaintext: str | None) -> bool:
        sql = f"UPDATE {column.table} SET {column.column} = ? WHERE id = ?"
        result = await self._session.execute(
            sql, (column.seal(self._vault, row_id, plaintext), row_id)
        )
        return result.rowcount > 0

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[None]:
        """Run the block all-or-nothing, in a new transaction or a savepoint in the caller's."""
        # A savepoint, so a caller that catches the block's error and commits cannot commit the
        # placeholder the block left behind.
        if self._session.in_transaction:
            async with self._session.savepoint():
                yield
        else:
            async with self._session.transaction():
                yield
