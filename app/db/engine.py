"""SQLite connection management and the async session handed to repositories."""

import asyncio
import contextvars
import random
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from app.config import Settings
from app.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BUSY_TIMEOUT_MS = 5000
DATABASE_WORKERS = 32
WAL_SIZE_LIMIT_BYTES = 4 * 1024 * 1024

Params = Sequence[Any] | Mapping[str, Any]
BeforeCommitHook = Callable[[], Awaitable[None]]
AfterCommitCallback = Callable[[], None]


class DatabaseError(Exception):
    """Raised when the database cannot be opened in the required mode."""


class DatabaseClosed(DatabaseError):
    """Raised when a session or connection is asked of a Database after aclose()."""


def connect(
    path: Path,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    *,
    create: bool = True,
    secure_delete: bool = False,
) -> sqlite3.Connection:
    """Open a connection with WAL, foreign keys, a busy timeout and name-addressable rows.

    Without create, a missing file raises sqlite3.OperationalError instead of being created.
    """
    uri = f"{path.absolute().as_uri()}?mode={'rwc' if create else 'rw'}"
    # isolation_level=None leaves transaction control to explicit BEGIN/COMMIT statements.
    # check_same_thread=False is safe because Session serialises every call on its connection.
    connection = sqlite3.connect(
        uri,
        timeout=busy_timeout_ms / 1000,
        isolation_level=None,
        check_same_thread=False,
        uri=True,
    )
    try:
        connection.row_factory = sqlite3.Row
        # The timeout goes first so the journal-mode switch also waits on a locked file.
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        connection.execute("PRAGMA trusted_schema = OFF")
        if secure_delete:
            connection.execute("PRAGMA secure_delete = ON")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise DatabaseError("the database file does not support WAL journal mode")
        connection.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES}")
        connection.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        connection.close()
        raise
    return connection


BUSY_RETRY_ATTEMPTS = 6
_BUSY_BACKOFF_SECONDS = 0.05


def is_busy_error(error: sqlite3.OperationalError) -> bool:
    """Report whether the error is a lock conflict that may clear if the work is repeated."""
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


async def retry_busy[T](operation: Callable[[], Awaitable[T]]) -> T:
    """Run the operation again, with backoff, while SQLite reports a lock conflict.

    The operation must be safe to repeat from the start, which holds for one statement and for a
    whole transaction block but not for a step inside a transaction that is still open.
    """
    for attempt in range(BUSY_RETRY_ATTEMPTS - 1):
        try:
            return await operation()
        except sqlite3.OperationalError as error:
            if not is_busy_error(error):
                raise
        ceiling = _BUSY_BACKOFF_SECONDS * 2**attempt
        await asyncio.sleep(ceiling / 2 + random.uniform(0, ceiling / 2))
    return await operation()


def _rollback_to(connection: sqlite3.Connection, name: str) -> None:
    # ROLLBACK TO keeps the savepoint on the stack, so it is released as well.
    connection.execute(f"ROLLBACK TO {name}")
    connection.execute(f"RELEASE {name}")


@dataclass(slots=True, eq=False)
class _CommitHook:
    run: BeforeCommitHook
    once: bool


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Outcome of a write statement."""

    rowcount: int
    last_row_id: int | None


class Session:
    """One connection, used from the event loop through worker threads."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        executor: Executor | None = None,
        shared: dict[type, Any] | None = None,
    ) -> None:
        self._connection = connection
        self._executor = executor
        self.shared: dict[type, Any] = {} if shared is None else shared
        # Taken inside the worker so a cancelled await cannot let two statements overlap.
        self._lock = threading.Lock()
        self._in_transaction = False
        self._savepoint_count = 0
        self._commit_hooks: list[_CommitHook] = []
        self._commit_callbacks: list[AfterCommitCallback] = []

    @property
    def in_transaction(self) -> bool:
        """Report whether a transaction opened by transaction() is active."""
        return self._in_transaction

    async def fetch_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        """Run a query and return its first row, or None."""

        def work(connection: sqlite3.Connection) -> sqlite3.Row | None:
            row: sqlite3.Row | None = connection.execute(sql, params).fetchone()
            return row

        return await self._run(work)

    async def fetch_limited(self, sql: str, params: Params, *, limit: int) -> list[sqlite3.Row]:
        """Run a query and return at most limit rows; no call fetches an unbounded result."""
        if limit < 1:
            raise ValueError("limit must be at least 1")

        def work(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            return connection.execute(sql, params).fetchmany(limit)

        return await self._run(work)

    async def execute(self, sql: str, params: Params = ()) -> WriteResult:
        """Run a write statement; outside a transaction it commits immediately."""

        def work(connection: sqlite3.Connection) -> WriteResult:
            cursor = connection.execute(sql, params)
            return WriteResult(cursor.rowcount, cursor.lastrowid)

        return await self._run(work)

    async def fetch_written(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        """Run a write statement with a RETURNING clause and return its first row, or None."""

        def work(connection: sqlite3.Connection) -> sqlite3.Row | None:
            # fetchall drains the statement, which is what commits it outside a transaction.
            rows: list[sqlite3.Row] = connection.execute(sql, params).fetchall()
            return rows[0] if rows else None

        return await self._run(work)

    @asynccontextmanager
    async def transaction(self, *, standalone: bool = False) -> AsyncIterator[None]:
        """Run the block in one write transaction, committing on success, rolling back on error.

        With standalone, hooks and callbacks registered before the block belong to an enclosing
        scope: they neither run at this COMMIT nor are spent by it.
        """
        if self._in_transaction:
            raise DatabaseError("transactions do not nest")
        enclosing = (self._commit_hooks, self._commit_callbacks)
        if standalone:
            self._commit_hooks, self._commit_callbacks = [], []
        try:
            async with self._write_transaction():
                yield
        finally:
            if standalone:
                self._commit_hooks, self._commit_callbacks = enclosing

    @asynccontextmanager
    async def _write_transaction(self) -> AsyncIterator[None]:
        # IMMEDIATE takes the write lock at BEGIN, so a busy database fails here.
        begin = asyncio.ensure_future(
            self._run(lambda connection: connection.execute("BEGIN IMMEDIATE"))
        )
        try:
            await asyncio.shield(begin)
        except asyncio.CancelledError:
            await self._abandon_begin(begin)
            raise
        self._in_transaction = True
        hooks: list[_CommitHook] = []
        try:
            yield
            hooks = list(self._commit_hooks)
            for hook in hooks:
                await hook.run()
        except BaseException:
            await self._finish("ROLLBACK")
            raise
        try:
            await self._finish("COMMIT")
        except BaseException:
            await self._discard_failed_commit()
            raise
        spent = [hook for hook in hooks if hook.once]
        self._commit_hooks = [hook for hook in self._commit_hooks if hook not in spent]
        for callback in list(self._commit_callbacks):
            callback()

    def before_commit(self, hook: BeforeCommitHook, *, once: bool = False) -> Callable[[], None]:
        """Run hook inside each transaction whose block completes, just before COMMIT.

        A hook that raises rolls the transaction back and the exception propagates. With once, the
        hook is dropped after the first COMMIT it precedes; a transaction that rolls back leaves it
        registered. The returned function removes the hook.
        """
        registered = _CommitHook(hook, once)
        self._commit_hooks.append(registered)

        def remove() -> None:
            if registered in self._commit_hooks:
                self._commit_hooks.remove(registered)

        return remove

    def after_commit(self, callback: AfterCommitCallback) -> Callable[[], None]:
        """Call callback after each COMMIT that succeeds; the returned function removes it."""
        self._commit_callbacks.append(callback)

        def remove() -> None:
            if callback in self._commit_callbacks:
                self._commit_callbacks.remove(callback)

        return remove

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Run the block inside the open transaction, undoing only its own writes on error."""
        if not self._in_transaction:
            raise DatabaseError("a savepoint needs an open transaction")
        self._savepoint_count += 1
        # The name is built from a counter, so it is safe to interpolate.
        name = f"dockmin_sp_{self._savepoint_count}"
        await self._run(lambda connection: connection.execute(f"SAVEPOINT {name}"))
        try:
            yield
        except BaseException:
            await asyncio.shield(self._run(lambda connection: _rollback_to(connection, name)))
            raise
        else:
            await self._run(lambda connection: connection.execute(f"RELEASE {name}"))

    async def close(self) -> None:
        """Close the connection, discarding any open transaction."""
        await self._run(lambda connection: connection.close())

    async def _finish(self, statement: str) -> None:
        async def work() -> None:
            try:
                await self._run(lambda connection: connection.execute(statement))
            finally:
                self._in_transaction = False

        # Shielded: an abandoned ROLLBACK would leave the flag cleared over a transaction SQLite
        # still holds open; the flag is cleared inside the shield for the same reason.
        await asyncio.shield(work())

    async def _abandon_begin(self, begin: asyncio.Future[sqlite3.Cursor]) -> None:
        async def work() -> None:
            # The worker thread runs BEGIN even after the caller is cancelled, and would otherwise
            # leave the write lock held by a session that believes it is outside a transaction.
            with suppress(BaseException):
                await begin
            if begin.cancelled() or begin.exception() is not None:
                return
            with suppress(sqlite3.Error):
                await self._run(lambda connection: connection.execute("ROLLBACK"))

        await asyncio.shield(work())

    async def _discard_failed_commit(self) -> None:
        async def work() -> None:
            # SQLite keeps the transaction open when COMMIT reports BUSY, and it would otherwise
            # swallow the next statement that expects to autocommit.
            with suppress(sqlite3.Error):
                await self._run(lambda connection: connection.execute("ROLLBACK"))

        await asyncio.shield(work())

    async def _run[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                return work(self._connection)

        context = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(self._executor, context.run, locked)


class Database:
    """A database file plus the settings every connection to it is opened with."""

    def __init__(
        self,
        path: Path,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        *,
        create: bool = False,
        secure_delete: bool = False,
        keep_wal: bool = False,
    ) -> None:
        """With create, the first connection may create a missing file; later ones require it.

        With keep_wal, one extra connection is held open, idle, from the first session until
        aclose(), so the WAL and shared-memory files persist.
        """
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self.secure_delete = secure_delete
        self._may_create = create
        self._keep_wal = keep_wal
        self.shared: dict[type, Any] = {}
        self._anchor_failed = False
        self._anchor: sqlite3.Connection | None = None
        self._anchor_lock = threading.Lock()
        self._closed = False
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._create_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        """Report whether aclose() has been called."""
        return self._closed

    @property
    def executor(self) -> ThreadPoolExecutor:
        """Return the worker threads every session of this database runs its statements on.

        Separate from the loop's default executor, so a stalled lookup or file read there cannot
        starve queries. Created on first use.
        """
        with self._lock:
            self._refuse_if_closed()
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=DATABASE_WORKERS, thread_name_prefix="dockmin-db"
                )
            return self._executor

    async def aclose(self) -> None:
        """Refuse every later session and connection, then stop the worker threads.

        Queued statements finish first; sessions already open must be closed by their owners, and
        before this call when the WAL files are to be gone once it returns.
        """
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
        await asyncio.to_thread(self._shut_down, executor)

    def _shut_down(self, executor: ThreadPoolExecutor | None) -> None:
        if executor is not None:
            executor.shutdown(wait=True)
        with self._anchor_lock:
            anchor, self._anchor = self._anchor, None
        if anchor is not None:
            anchor.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        """Build a Database for the configured data directory, creating the file on first open."""
        return cls(settings.db_path, settings.db_busy_timeout_ms, create=True, keep_wal=True)

    def connect(self) -> sqlite3.Connection:
        """Open a raw connection; the caller closes it."""
        self._refuse_if_closed()
        if self._may_create:
            with self._create_lock:
                connection = self._open(create=self._may_create)
                self._may_create = False
        else:
            connection = self._open(create=False)
        try:
            self._refuse_if_closed()
        except DatabaseClosed:
            connection.close()
            raise
        return connection

    def _open(self, *, create: bool) -> sqlite3.Connection:
        return connect(
            self.path, self.busy_timeout_ms, create=create, secure_delete=self.secure_delete
        )

    def _connect_session(self) -> sqlite3.Connection:
        connection = self.connect()
        if self._keep_wal:
            self._hold_anchor()
        return connection

    def _hold_anchor(self) -> None:
        with self._anchor_lock:
            if self._anchor is not None or self._closed:
                return
            try:
                self._anchor = self._open(create=False)
            except (sqlite3.Error, DatabaseError) as error:
                if not self._anchor_failed:
                    self._anchor_failed = True
                    logger.warning(
                        "database_wal_hold_failed", extra={"error": type(error).__name__}
                    )

    def _refuse_if_closed(self) -> None:
        if self._closed:
            raise DatabaseClosed("the database has been closed")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Session]:
        """Open a session and close it when the block exits."""
        executor = self.executor
        try:
            connection = await asyncio.get_running_loop().run_in_executor(
                executor, self._connect_session
            )
        except RuntimeError:
            # A shut-down executor refuses new work with a bare RuntimeError.
            if self._closed:
                raise DatabaseClosed("the database has been closed") from None
            raise
        session = Session(connection, executor, self.shared)
        try:
            yield session
        finally:
            await session.close()


async def get_session(request: Request) -> AsyncIterator[Session]:
    """Provide one session per request."""
    database: Database = request.app.state.database
    async with database.session() as session:
        yield session
