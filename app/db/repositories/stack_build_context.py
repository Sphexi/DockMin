"""Repository for the build contexts of a stack: the only writer of stack_build_context."""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from app.db.engine import DatabaseError, Session
from app.db.repositories.clocked import Clock, ClockedRepository, utc_now
from app.db.repositories.errors import (
    MissingReferenceError,
    RepositoryError,
    constraints_as_domain_errors,
)

if TYPE_CHECKING:
    from app.db.repositories.stack import StackRepository

__all__ = [
    "MAX_CONTEXTS",
    "MAX_CONTEXT_EXTRACTED_BYTES",
    "MAX_CONTEXT_MEMBERS",
    "MAX_CONTEXT_UPLOAD_BYTES",
    "MAX_STACK_CONTEXT_BYTES",
    "ContextArchive",
    "ContextWrite",
    "StackContextLimitError",
    "StackContextRepository",
    "StoredContext",
]

MAX_CONTEXT_UPLOAD_BYTES: Final = 16 * 1024 * 1024
MAX_STACK_CONTEXT_BYTES: Final = 64 * 1024 * 1024
MAX_CONTEXT_EXTRACTED_BYTES: Final = 256 * 1024 * 1024
MAX_CONTEXT_MEMBERS: Final = 10_000
MAX_CONTEXTS: Final = 32

_TABLE = "stack_build_context"
_LISTED = (
    "name, content_digest, compressed, member_count, extracted_size, "
    "length(content) AS size_bytes, created_at, updated_at"
)


class StackContextLimitError(RepositoryError):
    """Raised when a write would leave a stack over the context count or the total size."""


@dataclass(frozen=True, slots=True)
class StoredContext:
    """A context as stored, without its archive; `size_bytes` is the archive as uploaded."""

    name: str
    content_digest: str
    compressed: bool
    member_count: int
    extracted_size: int
    size_bytes: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ContextArchive:
    """A context's archive exactly as stored."""

    name: str
    content: bytes = field(repr=False)
    content_digest: str
    compressed: bool


@dataclass(frozen=True, slots=True)
class ContextWrite:
    """A validated archive ready to store."""

    content: bytes = field(repr=False)
    content_digest: str
    compressed: bool
    member_count: int
    extracted_size: int


class StackContextRepository(ClockedRepository[StoredContext]):
    """Reads and writes build contexts; its writes refresh the stack's inputs digest."""

    def __init__(
        self, session: Session, clock: Clock = utc_now, *, stacks: "StackRepository"
    ) -> None:
        super().__init__(session, clock)
        self._stacks = stacks

    def _map_row(self, row: sqlite3.Row) -> StoredContext:
        return StoredContext(
            name=str(row["name"]),
            content_digest=str(row["content_digest"]),
            compressed=bool(row["compressed"]),
            member_count=int(row["member_count"]),
            extracted_size=int(row["extracted_size"]),
            size_bytes=int(row["size_bytes"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @asynccontextmanager
    async def _atomic(self) -> AsyncIterator[None]:
        # A savepoint, not a bare join: a caller that catches the error and commits must not
        # commit what the block left behind.
        if self._session.in_transaction:
            async with self._session.savepoint():
                yield
        else:
            async with self._session.transaction():
                yield

    async def contexts(self, stack_id: int) -> list[StoredContext]:
        """List a stack's contexts by name, without their archives."""
        rows = await self._session.fetch_limited(
            f"SELECT {_LISTED} FROM {_TABLE} WHERE stack_id = ? ORDER BY name LIMIT ?",
            (stack_id, MAX_CONTEXTS + 1),
            limit=MAX_CONTEXTS + 1,
        )
        return [self._map_row(row) for row in rows]

    async def get(self, stack_id: int, name: str) -> StoredContext | None:
        """Fetch one context by name, ignoring case, without its archive."""
        return await self._one(
            f"SELECT {_LISTED} FROM {_TABLE} WHERE stack_id = ? AND name = ?", (stack_id, name)
        )

    async def archive(self, stack_id: int, name: str) -> ContextArchive | None:
        """Fetch one context's archive as stored, or None."""
        row = await self._session.fetch_one(
            f"SELECT name, content, content_digest, compressed FROM {_TABLE} "
            "WHERE stack_id = ? AND name = ?",
            (stack_id, name),
        )
        if row is None:
            return None
        return ContextArchive(
            name=str(row["name"]),
            content=bytes(row["content"]),
            content_digest=str(row["content_digest"]),
            compressed=bool(row["compressed"]),
        )

    async def put(self, stack_id: int, name: str, write: ContextWrite) -> StoredContext:
        """Add the context or replace the one of that name, and recompute the inputs digest.

        A replacement keeps the name as first stored. StackContextLimitError when the write would
        leave the stack over MAX_CONTEXTS or MAX_STACK_CONTEXT_BYTES, which changes nothing.
        MissingReferenceError if the stack does not exist.
        """
        stamp = self._now()
        with constraints_as_domain_errors("stack build context"):
            async with self._atomic():
                if (
                    await self._session.fetch_one("SELECT 1 FROM stack WHERE id = ?", (stack_id,))
                    is None
                ):
                    raise MissingReferenceError(
                        "stack build context refers to a row that does not exist"
                    )
                existing = await self._session.fetch_one(
                    f"SELECT name FROM {_TABLE} WHERE stack_id = ? AND name = ?", (stack_id, name)
                )
                if existing is None:
                    await self._execute(
                        f"INSERT INTO {_TABLE} (stack_id, name, content, content_digest, "
                        "compressed, member_count, extracted_size, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            stack_id,
                            name,
                            write.content,
                            write.content_digest,
                            int(write.compressed),
                            write.member_count,
                            write.extracted_size,
                            stamp,
                            stamp,
                        ),
                    )
                    stored_name = name
                else:
                    stored_name = str(existing["name"])
                    await self._execute(
                        f"UPDATE {_TABLE} SET content = ?, content_digest = ?, compressed = ?, "
                        "member_count = ?, extracted_size = ?, updated_at = ? "
                        "WHERE stack_id = ? AND name = ?",
                        (
                            write.content,
                            write.content_digest,
                            int(write.compressed),
                            write.member_count,
                            write.extracted_size,
                            stamp,
                            stack_id,
                            stored_name,
                        ),
                    )
                await self._require_within_limits(stack_id)
                await self._stacks.refresh_inputs_digest(stack_id, touched_at=stamp)
        stored = await self.get(stack_id, stored_name)
        if stored is None:
            raise DatabaseError("a context written by this call could not be read back")
        return stored

    async def delete(self, stack_id: int, name: str) -> bool:
        """Delete a context and recompute the inputs digest; False if there is no such context."""
        async with self._atomic():
            result = await self._execute(
                f"DELETE FROM {_TABLE} WHERE stack_id = ? AND name = ?", (stack_id, name)
            )
            if result.rowcount == 0:
                return False
            await self._stacks.refresh_inputs_digest(stack_id, touched_at=self._now())
        return True

    async def _require_within_limits(self, stack_id: int) -> None:
        row = await self._session.fetch_one(
            f"SELECT count(*) AS total, COALESCE(sum(length(content)), 0) AS bytes FROM {_TABLE} "
            "WHERE stack_id = ?",
            (stack_id,),
        )
        if row is not None and (
            int(row["total"]) > MAX_CONTEXTS or int(row["bytes"]) > MAX_STACK_CONTEXT_BYTES
        ):
            raise StackContextLimitError(
                "a stack holds at most 32 build contexts and 64 MiB of archives in total"
            )
