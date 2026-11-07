"""Repository for the companion files of a stack: the only writer of stack_file."""

import hashlib
import sqlite3
from dataclasses import dataclass, field
from secrets import choice
from typing import TYPE_CHECKING, Final

from app.crypto.envelope import DecryptionError
from app.crypto.registry import STACK_FILE_CONTENT
from app.crypto.vault import Vault
from app.db.engine import DatabaseError, Session
from app.db.repositories.clocked import Clock, utc_now
from app.db.repositories.encrypted import EncryptedStr, SecretRepository
from app.db.repositories.errors import (
    MissingReferenceError,
    RepositoryError,
    constraints_as_domain_errors,
)
from app.db.repositories.registry_credential import SecretUnreadableError

__all__ = [
    "MAX_FILES",
    "MAX_FILES_TOTAL_BYTES",
    "MAX_FILE_BYTES",
    "StackFileError",
    "StackFileLimitError",
    "StackFileRepository",
    "StoredFile",
]

MAX_FILES: Final = 20
MAX_FILE_BYTES: Final = 65536
MAX_FILES_TOTAL_BYTES: Final = 131072

_TOKEN_LENGTH: Final = 12
_TOKEN_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"
_CONTENT = EncryptedStr(STACK_FILE_CONTENT)
_TABLE = "stack_file"
_COLUMNS = (
    "id, name, is_secret, content, content_digest, size_bytes, value_token, created_at, updated_at"
)

if TYPE_CHECKING:
    from app.db.repositories.stack import StackRepository


class StackFileError(RepositoryError):
    """Raised when a file write cannot be carried out, for example a secret with no content."""


class StackFileLimitError(StackFileError):
    """Raised when a write would leave a stack over the file count or the total size."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    """A companion file as stored; a secret file's content is never carried, only that it is set."""

    id: int
    name: str
    is_secret: bool
    content: str | None = field(repr=False)
    content_digest: str | None
    size_bytes: int
    value_token: str
    created_at: str
    updated_at: str


def _new_token() -> str:
    return "".join(choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class StackFileRepository(SecretRepository[StoredFile]):
    """Reads and writes companion files; the only code that seals or opens their secret content."""

    _secret_columns = (_CONTENT,)

    def __init__(
        self, session: Session, vault: Vault, clock: Clock = utc_now, *, stacks: "StackRepository"
    ) -> None:
        super().__init__(session, vault, clock)
        self._stacks = stacks

    def _map_row(self, row: sqlite3.Row) -> StoredFile:
        return StoredFile(
            id=int(row["id"]),
            name=str(row["name"]),
            is_secret=bool(row["is_secret"]),
            content=row["content"],
            content_digest=row["content_digest"],
            size_bytes=int(row["size_bytes"]),
            value_token=str(row["value_token"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def files(self, stack_id: int) -> list[StoredFile]:
        """List a stack's files by name; a secret file is present by name, size and token only."""
        rows = await self._session.fetch_limited(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE stack_id = ? ORDER BY name LIMIT ?",
            (stack_id, MAX_FILES + 1),
            limit=MAX_FILES + 1,
        )
        return [self._map_row(row) for row in rows]

    async def reveal_files(self, stack_id: int) -> dict[str, str]:
        """Decrypt every secret file of a stack, by file name.

        SecretUnreadableError if any content cannot be decrypted with the keys this process holds.
        """
        rows = await self._session.fetch_limited(
            f"SELECT id, name, content_ciphertext FROM {_TABLE} "
            "WHERE stack_id = ? AND is_secret = 1 ORDER BY name LIMIT ?",
            (stack_id, MAX_FILES + 1),
            limit=MAX_FILES + 1,
        )
        revealed: dict[str, str] = {}
        for row in rows:
            try:
                content = self._reveal(_CONTENT, row)
            except DecryptionError:
                raise SecretUnreadableError("a stored file cannot be read") from None
            if content is None:
                raise SecretUnreadableError("a stored file cannot be read")
            revealed[str(row["name"])] = content
        return revealed

    async def put_file(
        self, stack_id: int, name: str, *, content: str | None, secret: bool
    ) -> StoredFile:
        """Create the file or replace its content, and recompute the stack's inputs digest.

        A secret given no content keeps the content already stored; StackFileError if there is
        none, or a plain file is given no content. A write that leaves the stack over the limits
        is StackFileLimitError and changes nothing. A new value_token is issued by every content
        write. DuplicateError if the name is taken ignoring case, MissingReferenceError if the
        stack does not exist.
        """
        stamp = self._now()
        with constraints_as_domain_errors("stack file"):
            async with self._atomic():
                existing = await self._session.fetch_one(
                    f"SELECT {_COLUMNS} FROM {_TABLE} WHERE stack_id = ? AND name = ?",
                    (stack_id, name),
                )
                if (
                    existing is None
                    and await self._session.fetch_one(
                        "SELECT 1 FROM stack WHERE id = ?", (stack_id,)
                    )
                    is None
                ):
                    raise MissingReferenceError("stack file refers to a row that does not exist")
                row_id = await self._write(stack_id, existing, name, content, secret, stamp)
                await self._require_within_limits(stack_id)
                if content is not None:
                    await self._stacks.refresh_inputs_digest(stack_id, touched_at=stamp)
        stored = await self._one(f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = ?", (row_id,))
        if stored is None:
            raise DatabaseError("a file written by this call could not be read back")
        return stored

    async def delete_file(self, stack_id: int, name: str) -> bool:
        """Delete a file and recompute the stack's inputs digest; False if there is no such file."""
        async with self._atomic():
            result = await self._execute(
                f"DELETE FROM {_TABLE} WHERE stack_id = ? AND name = ?", (stack_id, name)
            )
            if result.rowcount == 0:
                return False
            await self._stacks.refresh_inputs_digest(stack_id, touched_at=self._now())
        return True

    async def _write(
        self,
        stack_id: int,
        existing: sqlite3.Row | None,
        name: str,
        content: str | None,
        secret: bool,
        stamp: str,
    ) -> int:
        if content is None:
            if existing is None or not (secret and bool(existing["is_secret"])):
                raise StackFileError(f"file {name} needs content")
            return int(existing["id"])
        size = len(content.encode("utf-8"))
        if secret:
            plain = {
                "is_secret": 1,
                "content": None,
                "content_digest": None,
                "size_bytes": size,
                "value_token": _new_token(),
            }
            secrets: dict[EncryptedStr, str | None] = {_CONTENT: content}
        else:
            plain = {
                "is_secret": 0,
                "content": content,
                "content_digest": _digest(content),
                "size_bytes": size,
                "value_token": _new_token(),
            }
            secrets = {_CONTENT: None}
        if existing is None:
            return await self._insert_with_secrets(
                _TABLE,
                {
                    "stack_id": stack_id,
                    "name": name,
                    **plain,
                    "created_at": stamp,
                    "updated_at": stamp,
                },
                secrets if secret else {},
            )
        row_id = int(existing["id"])
        await self._update_with_secrets(_TABLE, row_id, {**plain, "updated_at": stamp}, secrets)
        return row_id

    async def _require_within_limits(self, stack_id: int) -> None:
        row = await self._session.fetch_one(
            f"SELECT count(*) AS total, COALESCE(sum(size_bytes), 0) AS bytes FROM {_TABLE} "
            "WHERE stack_id = ?",
            (stack_id,),
        )
        if row is not None and (
            int(row["total"]) > MAX_FILES or int(row["bytes"]) > MAX_FILES_TOTAL_BYTES
        ):
            raise StackFileLimitError("a stack holds at most 20 files and 128 KiB in total")
