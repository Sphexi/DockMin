"""The stored-secret overview, the read-only check, and the re-key run under the offline gate."""

import asyncio
import contextvars
import hashlib
import json
import secrets
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from app.auth.errors import NotFound, ProductError, ValidationFailed
from app.auth.principal import Principal
from app.config import KeySource, Settings
from app.crypto.keys import key_fingerprint
from app.crypto.registry import SECRET_FIELDS
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.db.repositories.crypto_state import CryptoStateRepository
from app.db.repositories.rekey import RekeyFailure, RekeyPass, RekeyProgress
from app.logging import get_logger
from app.scheduler.loop import Scheduler
from app.services.activity import ActivityRecorder, UserActor, audited, completed, current_audit
from app.services.backups import BackupProduction
from app.services.guards import require_administrator
from app.services.restore import UPDATE_IN_PROGRESS, RestoreRuntime, update_files_present
from app.services.restore_gate import RestoreGate

type RunKind = Literal["check", "rekey"]
type RunPhase = Literal["running", "finished"]
type RunOutcome = Literal["complete", "values_unreadable", "interrupted", "failed"]

logger = get_logger(__name__)

__all__ = [
    "ColumnCounts",
    "EncryptionConflict",
    "EncryptionOverview",
    "EncryptionService",
    "PreviousKey",
    "RekeyLog",
    "RekeyRun",
    "RekeyRunNotFound",
    "counts_digest",
]

VALUES_CHECKED_ACTION: Final = "encryption_values_checked"
REKEY_REQUESTED_ACTION: Final = "encryption_rekey_requested"
REKEY_COMPLETED_ACTION: Final = "encryption_rekey_completed"
TARGET_TYPE: Final = "instance"

CONFIRM_FIELD: Final = "confirm_rekey"
CONFIRM_MESSAGE: Final = "Confirm that stored secrets will be re-encrypted."
RUN_ACTIVE_MESSAGE: Final = "A check or re-encryption is already running."
RESTORE_ACTIVE_MESSAGE: Final = "A restore is in progress. Try again when it has finished."
BACKUP_BUSY_MESSAGE: Final = "A backup is still being produced. Try again shortly."
STALE_COUNTS_MESSAGE: Final = "Changed since it was reviewed; nothing was rewritten."
STOPPING_MESSAGE: Final = "The instance is shutting down."

ENGAGE_WAIT_SECONDS: Final = 30.0
MAX_FAILURE_LOCATIONS: Final = 100
FINISHED_KEPT: Final = timedelta(minutes=30)
RUN_ID_BYTES: Final = 16


class EncryptionConflict(ProductError):
    """The operation cannot start now; the message is fixed product text."""


class RekeyRunNotFound(NotFound):
    """No check or re-encryption has this id, or it ended long enough ago to be forgotten."""


@dataclass(frozen=True, slots=True)
class ColumnCounts:
    """The well-formed values of one secret column at each key version, lowest first."""

    table: str
    column: str
    by_version: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class PreviousKey:
    """The previous key the instance loaded, by where it comes from and its fingerprint."""

    source: str
    fingerprint: str


@dataclass(slots=True)
class RekeyRun:
    """One check or re-encryption, kept in memory while it runs and for a while after."""

    id: str
    kind: RunKind
    key_version: int
    started_at: datetime
    phase: RunPhase = "running"
    outcome: RunOutcome | None = None
    rewritten: int = 0
    checked: int = 0
    remaining_by_version: dict[int, int] = field(default_factory=dict)
    unreadable: int = 0
    corrupt: int = 0
    unparseable: int = 0
    failures: tuple[RekeyFailure, ...] = ()
    finished_at: datetime | None = None

    @property
    def remaining_below_current(self) -> int:
        """Return how many values were last seen below the key version of the run."""
        return _below(self.remaining_by_version, self.key_version)


@dataclass(frozen=True, slots=True)
class EncryptionOverview:
    """What the Encryption page shows."""

    key_source: KeySource
    key_description: str
    key_version: int
    key_fingerprint: str
    previous_key: PreviousKey | None
    columns: tuple[ColumnCounts, ...]
    values_below_current: int
    counts_digest: str
    last_run: RekeyRun | None


@dataclass(slots=True)
class _Hold:
    run: RekeyRun
    vault: Vault
    database: Database
    values_below: int
    holds_production: bool = False
    engaged: bool = False
    holder: asyncio.Task[None] | None = None
    release: asyncio.Event = field(default_factory=asyncio.Event)


def counts_digest(counts: Mapping[tuple[str, str], Mapping[int, int]]) -> str:
    """Return a digest of the value counts per secret column and key version."""
    canonical = [
        [table, column, sorted(by_version.items())]
        for (table, column), by_version in sorted(counts.items())
    ]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode("utf-8")).hexdigest()


def _below(by_version: Mapping[int, int], version: int) -> int:
    return sum(total for found, total in by_version.items() if found < version)


def _below_in(counts: Mapping[tuple[str, str], Mapping[int, int]], version: int) -> int:
    return sum(_below(by_version, version) for by_version in counts.values())


def _by_version(counts: Mapping[tuple[str, str], Mapping[int, int]]) -> dict[int, int]:
    totals: dict[int, int] = {}
    for by_version in counts.values():
        for version, total in by_version.items():
            totals[version] = totals.get(version, 0) + total
    return totals


def _apply(run: RekeyRun, progress: RekeyProgress) -> None:
    run.rewritten = progress.rewritten_total
    run.checked = progress.checked_total
    run.remaining_by_version = dict(progress.remaining_by_version)
    kinds = [failure.kind for failure in progress.failures]
    run.unreadable = kinds.count("unreadable")
    run.corrupt = kinds.count("corrupt")
    run.unparseable = kinds.count("unparseable")
    run.failures = progress.failures[:MAX_FAILURE_LOCATIONS]


def _outcome_of(run: RekeyRun, *, complete: bool) -> RunOutcome:
    if not complete:
        return "interrupted"
    unopened = run.unreadable + run.corrupt + run.unparseable
    if unopened or (run.kind == "rekey" and run.remaining_below_current):
        return "values_unreadable"
    return "complete"


def _file_task(task: asyncio.Task[None], tasks: set[asyncio.Task[None]]) -> None:
    tasks.add(task)
    task.add_done_callback(tasks.discard)


class RekeyLog:
    """Writes the row that ends a re-key, on a session of its own."""

    def __init__(self, db: Session, clock: Clock) -> None:
        self._db = db
        self._activity = ActivityRecorder(db, clock)

    async def finished(
        self, actor_id: int, detail: Mapping[str, DetailValue], *, reason: str | None
    ) -> None:
        """Record how a re-key ended; a row with a reason is a failure."""
        body = dict(detail)
        if reason is not None:
            body["reason"] = reason
        async with self._db.transaction():
            await self._activity.record_event(
                UserActor(actor_id),
                action=REKEY_COMPLETED_ACTION,
                target_type=TARGET_TYPE,
                result="success" if reason is None else "failure",
                detail=body,
            )


class _Operations:
    """The audited operations, each on a session of its own."""

    def __init__(
        self,
        db: Session,
        clock: Clock,
        admit: Callable[..., Coroutine[Any, Any, _Hold]],
        release: Callable[[_Hold], Coroutine[Any, Any, None]],
    ) -> None:
        self._db = db
        self._admit = admit
        self._release = release
        self._activity = ActivityRecorder(db, clock)

    @audited(action=VALUES_CHECKED_ACTION, target_type=TARGET_TYPE)
    async def check_values(self, actor: Principal, *, run: RekeyRun, vault: Vault) -> None:
        """Open every stored secret and record how many did not open."""
        audit = current_audit()
        async for progress in RekeyPass(self._db, vault).verify():
            _apply(run, progress)
        audit.detail.update(checked=run.checked, corrupt=run.corrupt, unparseable=run.unparseable)

    @audited(action=REKEY_REQUESTED_ACTION, target_type=TARGET_TYPE)
    async def request_rekey(
        self, actor: Principal, *, expected_counts: str, confirmed: bool, runtime: RestoreRuntime
    ) -> _Hold:
        """Take the instance offline for a re-key and commit the request row."""
        audit = current_audit()
        hold = await self._admit(
            self._db, runtime, expected_counts=expected_counts, confirmed=confirmed
        )
        try:
            async with self._db.transaction():
                audit.detail.update(
                    key_version=hold.run.key_version, values_below_current=hold.values_below
                )
        except BaseException:
            await completed(self._release(hold))
            raise
        return hold


class EncryptionService:
    """Reports on stored secrets, checks them, and re-encrypts them; administrators only."""

    def __init__(
        self,
        *,
        gate: RestoreGate,
        production: BackupProduction,
        data_dir: Path,
        runtime: Callable[[], RestoreRuntime],
        settings: Settings,
        clock: Clock,
        wait_seconds: float = ENGAGE_WAIT_SECONDS,
    ) -> None:
        self._gate = gate
        self._production = production
        self._data_dir = data_dir
        self._runtime = runtime
        self._settings = settings
        self._clock = clock
        self._wait_seconds = wait_seconds
        self._runs: dict[str, RekeyRun] = {}
        self._active: RekeyRun | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._check_tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def overview(self, actor: Principal) -> EncryptionOverview:
        """Return the key in use, the previous key, and the value counts per column and version."""
        require_administrator(actor)
        self._expire()
        runtime = self._runtime()
        async with runtime.database.session() as db:
            counts = await CryptoStateRepository(db).count_by_field_and_version()
        version = runtime.vault.current_version
        previous = self._settings.previous_encryption_key
        latest = next(reversed(self._runs.values()), None)
        return EncryptionOverview(
            key_source=self._settings.key_source,
            key_description=self._settings.key_description,
            key_version=version,
            key_fingerprint=runtime.vault.key_fingerprint(),
            previous_key=(
                None
                if previous is None
                else PreviousKey(self._settings.previous_key_description, key_fingerprint(previous))
            ),
            columns=tuple(
                ColumnCounts(
                    secret.table,
                    secret.column,
                    tuple(sorted(counts.get((secret.table, secret.column), {}).items())),
                )
                for secret in SECRET_FIELDS
            ),
            values_below_current=_below_in(counts, version),
            counts_digest=counts_digest(counts),
            last_run=None if latest is None else self._snapshot(latest),
        )

    async def run(self, actor: Principal, run_id: str) -> RekeyRun:
        """Return the state of a check or re-encryption; raises RekeyRunNotFound if unknown."""
        require_administrator(actor)
        self._expire()
        found = self._runs.get(run_id)
        if found is None:
            raise RekeyRunNotFound("no such run")
        return self._snapshot(found)

    async def check(self, actor: Principal) -> str:
        """Start a read-only check of every stored secret and return its run id."""
        require_administrator(actor)
        self._require_free()
        runtime = self._runtime()
        run = self._claim("check", runtime.vault.current_version)
        task = self._spawn(self._run_check(actor, run, runtime))
        _file_task(task, self._check_tasks)
        return run.id

    async def begin_rekey(self, actor: Principal, *, expected_counts: str, confirmed: bool) -> str:
        """Take the instance offline and start re-encrypting; return the run id.

        Raises ValidationFailed on confirm_rekey when it is not confirmed, and EncryptionConflict
        when nothing needs rewriting, another operation is running, or the counts changed since
        they were reviewed.
        """
        require_administrator(actor)
        runtime = self._runtime()
        hold: _Hold | None = None
        try:
            async with runtime.database.session() as db:
                hold = await self._operations(db).request_rekey(
                    actor, expected_counts=expected_counts, confirmed=confirmed, runtime=runtime
                )
            self._spawn(self._run_rekey(actor.user_id, hold))
        except BaseException:
            if hold is not None:
                await completed(self._release(hold))
            raise
        return hold.run.id

    def check_running(self) -> bool:
        """Report whether a stored-secret check is running."""
        return any(not task.done() for task in self._check_tasks)

    async def aclose(self) -> None:
        """Stop the work in progress: a check is cancelled, a re-key stops between batches."""
        self._stopping.set()
        checks = list(self._check_tasks)
        for task in checks:
            task.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _admit(
        self, db: Session, runtime: RestoreRuntime, *, expected_counts: str, confirmed: bool
    ) -> _Hold:
        """Check every precondition in order, then engage the gate and suspend the scheduler.

        Whatever was taken is released when this raises.
        """
        if not confirmed:
            raise ValidationFailed({CONFIRM_FIELD: CONFIRM_MESSAGE})
        state = CryptoStateRepository(db)
        version = runtime.vault.current_version
        if not _below_in(await state.count_by_field_and_version(), version):
            raise EncryptionConflict(f"Every stored secret is already at key version {version}.")
        self._require_free()
        run = self._claim("rekey", version)
        hold = _Hold(run, runtime.vault, runtime.database, 0)
        try:
            if await asyncio.to_thread(update_files_present, self._data_dir):
                raise EncryptionConflict(UPDATE_IN_PROGRESS)
            self._require_no_jobs(runtime)
            await self._take_production(hold)
            self._require_gate_open()
            counts = await state.count_by_field_and_version()
            if counts_digest(counts) != expected_counts:
                raise EncryptionConflict(STALE_COUNTS_MESSAGE)
            run.remaining_by_version = _by_version(counts)
            hold.values_below = _below(run.remaining_by_version, version)
            self._gate.engage(run.id, self._clock(), purpose="rekey")
            hold.engaged = True
            self._require_no_jobs(runtime)
            entered = asyncio.Event()
            hold.holder = self._spawn(self._hold_scheduler(runtime.scheduler, entered, hold))
            await self._await_entered(hold.holder, entered)
        except BaseException:
            await completed(self._release(hold))
            raise
        return hold

    async def _release(self, hold: _Hold) -> None:
        """Give back everything a re-key took and forget the claim."""
        try:
            await self._teardown(hold)
        finally:
            self._runs.pop(hold.run.id, None)
            if self._active is hold.run:
                self._active = None

    async def _teardown(self, hold: _Hold) -> None:
        """Release the scheduler suspension, then the backup hold, then the gate."""
        hold.release.set()
        try:
            if hold.holder is not None:
                await asyncio.gather(hold.holder, return_exceptions=True)
        finally:
            if hold.holds_production:
                hold.holds_production = False
                self._production.free()
            if hold.engaged:
                hold.engaged = False
                self._gate.reopen()

    def _operations(self, db: Session) -> _Operations:
        return _Operations(db, self._clock, self._admit, self._release)

    def _claim(self, kind: RunKind, version: int) -> RekeyRun:
        run = RekeyRun(secrets.token_hex(RUN_ID_BYTES), kind, version, self._clock())
        self._active = run
        self._runs[run.id] = run
        return run

    def _require_free(self) -> None:
        if self._stopping.is_set():
            raise EncryptionConflict(STOPPING_MESSAGE)
        if self._active is not None or (
            self._gate.phase != "open" and self._gate.purpose == "rekey"
        ):
            raise EncryptionConflict(RUN_ACTIVE_MESSAGE)
        self._require_gate_open()

    def _require_gate_open(self) -> None:
        if self._gate.phase != "open":
            raise EncryptionConflict(RESTORE_ACTIVE_MESSAGE)

    @staticmethod
    def _require_no_jobs(runtime: RestoreRuntime) -> None:
        running = runtime.image_jobs.running_count() + runtime.stack_jobs.running_count()
        if running:
            raise EncryptionConflict(f"{running} jobs are running; wait for them to finish.")

    async def _take_production(self, hold: _Hold) -> None:
        if self._production.try_hold(for_rekey=True):
            hold.holds_production = True
            return
        try:
            async with asyncio.timeout(self._wait_seconds):
                await self._production.hold(for_rekey=True)
        except TimeoutError:
            raise EncryptionConflict(BACKUP_BUSY_MESSAGE) from None
        hold.holds_production = True

    @staticmethod
    async def _hold_scheduler(scheduler: Scheduler, entered: asyncio.Event, hold: _Hold) -> None:
        async with scheduler.suspend():
            entered.set()
            await hold.release.wait()

    @staticmethod
    async def _await_entered(holder: asyncio.Task[None], entered: asyncio.Event) -> None:
        waiter = asyncio.ensure_future(entered.wait())
        try:
            await asyncio.wait({holder, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if holder.done():
            holder.result()
            raise RuntimeError("the scheduler suspension ended before it was needed")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.get_running_loop().create_task(work, context=contextvars.Context())
        _file_task(task, self._tasks)
        return task

    async def _run_check(self, actor: Principal, run: RekeyRun, runtime: RestoreRuntime) -> None:
        complete = False
        try:
            async with runtime.database.session() as db:
                await self._operations(db).check_values(actor, run=run, vault=runtime.vault)
            complete = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("encryption_check_failed", extra={"error": type(error).__name__})
            self._end(run, "failed")
        finally:
            if run.phase == "running":
                self._end(run, _outcome_of(run, complete=complete))

    async def _run_rekey(self, actor_id: int, hold: _Hold) -> None:
        run = hold.run
        started = asyncio.get_running_loop().time()
        complete = False
        try:
            async with hold.database.session() as db:
                stream = RekeyPass(db, hold.vault, verify_current=True).run()
                try:
                    async for progress in stream:
                        _apply(run, progress)
                        complete = progress.complete
                        if self._stopping.is_set():
                            break
                finally:
                    await stream.aclose()
        except Exception as error:
            logger.error("encryption_rekey_failed", extra={"error": type(error).__name__})
        finally:
            elapsed = asyncio.get_running_loop().time() - started
            await completed(self._finish_rekey(actor_id, hold, complete, elapsed))

    async def _finish_rekey(
        self, actor_id: int, hold: _Hold, complete: bool, elapsed: float
    ) -> None:
        run = hold.run
        outcome = _outcome_of(run, complete=complete)
        remaining = run.remaining_below_current
        try:
            await self._write_completed(actor_id, hold, outcome, remaining, elapsed)
        finally:
            try:
                await self._teardown(hold)
            finally:
                self._end(run, outcome)
        logger.info(
            "encryption_rekey_finished",
            extra={"outcome": outcome, "rewritten": run.rewritten, "remaining": remaining},
        )

    async def _write_completed(
        self, actor_id: int, hold: _Hold, outcome: RunOutcome, remaining: int, elapsed: float
    ) -> None:
        run = hold.run
        detail: dict[str, DetailValue] = {
            "key_version": run.key_version,
            "rewritten": run.rewritten,
            "checked": run.checked,
            "unreadable": run.unreadable,
            "corrupt": run.corrupt,
            "unparseable": run.unparseable,
            "remaining_below_current": remaining,
            "duration_ms": int(elapsed * 1000),
        }
        try:
            async with hold.database.session() as db:
                await RekeyLog(db, self._clock).finished(
                    actor_id, detail, reason=None if outcome == "complete" else outcome
                )
        except Exception as error:
            logger.error("encryption_rekey_row_write_failed", extra={"error": type(error).__name__})

    def _end(self, run: RekeyRun, outcome: RunOutcome) -> None:
        if run.phase == "finished":
            return
        run.phase = "finished"
        run.outcome = outcome
        run.finished_at = self._clock()
        if self._active is run:
            self._active = None

    def _expire(self) -> None:
        now = self._clock()
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if run.finished_at is not None and now - run.finished_at > FINISHED_KEPT
        ]
        for run_id in stale:
            del self._runs[run_id]

    @staticmethod
    def _snapshot(run: RekeyRun) -> RekeyRun:
        return replace(run, remaining_by_version=dict(run.remaining_by_version))
