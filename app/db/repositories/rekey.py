"""The resumable pass that rewrites secret values at the current key version."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal

from app.crypto.envelope import DecryptionError, SecretLocation
from app.crypto.registry import SECRET_FIELDS, SecretField
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.crypto_state import CryptoStateRepository, version_expression

DEFAULT_BATCH_SIZE = 100

FailureKind = Literal["unreadable", "corrupt", "unparseable"]


@dataclass(frozen=True, slots=True)
class RekeyFailure:
    """A value the pass could not rewrite or open; it names the location, never the value."""

    field: SecretField
    row_id: int
    kind: FailureKind = "unreadable"


@dataclass(frozen=True, slots=True)
class RekeyProgress:
    """State after one committed batch."""

    rewritten_total: int
    remaining_by_version: dict[int, int]
    failures: tuple[RekeyFailure, ...] = field(default=())
    complete: bool = False
    checked_total: int = 0


class RekeyPass:
    """Rewrites every secret value not yet at the vault's current key version.

    Each batch is its own transaction, so stopping between batches, by ceasing to iterate or by
    cancelling, leaves every value readable at whichever version it holds. Running the pass
    again resumes: finished values do not match the selection.

    With verify_current, run also opens every well-formed value already at the current version
    and reports those that do not open as corrupt, and values not shaped like ciphertext as
    unparseable; none of them are rewritten. Verify does the same and writes nothing.
    """

    def __init__(
        self,
        session: Session,
        vault: Vault,
        batch_size: int = DEFAULT_BATCH_SIZE,
        *,
        verify_current: bool = False,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._session = session
        self._vault = vault
        self._batch_size = batch_size
        self._verify_current = verify_current
        self._state = CryptoStateRepository(session)

    async def remaining_by_version(self) -> dict[int, int]:
        """Return the count of values at each key version."""
        return await self._state.count_by_version()

    async def run(self) -> AsyncGenerator[RekeyProgress, None]:
        """Yield progress after each committed batch; the last item has complete set."""
        rewritten = 0
        checked = 0
        failures: list[RekeyFailure] = []
        for secret_field in SECRET_FIELDS:
            last_id = 0
            while True:
                done, last_id, failed = await self._batch(secret_field, last_id)
                rewritten += done
                failures.extend(failed)
                exhausted = last_id < 0
                yield RekeyProgress(
                    rewritten,
                    await self.remaining_by_version(),
                    tuple(failures),
                    complete=False,
                    checked_total=checked,
                )
                if exhausted:
                    break
        if self._verify_current:
            async for opened, found in self._verification(include_unparseable=True):
                checked += opened
                found_at = {(item.field, item.row_id) for item in found}
                failures[:] = [
                    item for item in failures if (item.field, item.row_id) not in found_at
                ]
                failures.extend(found)
                yield RekeyProgress(
                    rewritten,
                    await self.remaining_by_version(),
                    tuple(failures),
                    complete=False,
                    checked_total=checked,
                )
        yield RekeyProgress(
            rewritten, await self.remaining_by_version(), tuple(failures), True, checked
        )

    async def verify(self) -> AsyncGenerator[RekeyProgress, None]:
        """Yield progress while opening every current-version value; nothing is written.

        The last item has complete set. rewritten_total stays 0 and failures are corrupt or
        unparseable.
        """
        checked = 0
        failures: list[RekeyFailure] = []
        async for opened, found in self._verification(include_unparseable=True):
            checked += opened
            failures.extend(found)
            yield RekeyProgress(
                0,
                await self.remaining_by_version(),
                tuple(failures),
                complete=False,
                checked_total=checked,
            )
        yield RekeyProgress(0, await self.remaining_by_version(), tuple(failures), True, checked)

    async def _verification(
        self, *, include_unparseable: bool
    ) -> AsyncGenerator[tuple[int, list[RekeyFailure]], None]:
        """Yield (values opened, failures) per batch, then the unparseable values if asked."""
        for secret_field in SECRET_FIELDS:
            last_id = 0
            while True:
                opened, last_id, failed = await self._open_batch(secret_field, last_id)
                yield opened, failed
                if last_id < 0:
                    break
        if not include_unparseable:
            return
        unparseable = await self._state.find_unparseable()
        yield 0, [RekeyFailure(item.field, item.row_id, "unparseable") for item in unparseable]

    async def _open_batch(
        self, secret_field: SecretField, after_id: int
    ) -> tuple[int, int, list[RekeyFailure]]:
        """Open one batch of current-version values; return (opened, last id or -1, failures)."""
        table, column = secret_field.table, secret_field.column
        select = (
            f"SELECT id, {column} AS stored FROM {table} "
            f"WHERE id > ? AND {column} LIKE ? AND {version_expression(column)} IS NOT NULL "
            "ORDER BY id LIMIT ?"
        )
        current_prefix = f"v{self._vault.current_version}:%"
        rows = await self._session.fetch_limited(
            select, (after_id, current_prefix, self._batch_size), limit=self._batch_size
        )
        failures: list[RekeyFailure] = []
        for row in rows:
            row_id = int(row["id"])
            try:
                self._vault.open(str(row["stored"]), SecretLocation(table, column, row_id))
            except DecryptionError:
                failures.append(RekeyFailure(secret_field, row_id, "corrupt"))
        if len(rows) < self._batch_size:
            return len(rows), -1, failures
        return len(rows), int(rows[-1]["id"]), failures

    async def _batch(
        self, secret_field: SecretField, after_id: int
    ) -> tuple[int, int, list[RekeyFailure]]:
        """Rewrite one batch; return (rewritten, last id seen or -1 when exhausted, failures)."""
        table, column = secret_field.table, secret_field.column
        select = (
            f"SELECT id, {column} AS stored FROM {table} "
            f"WHERE id > ? AND {column} IS NOT NULL AND {column} NOT LIKE ? ORDER BY id LIMIT ?"
        )
        update = f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} = ?"
        current_prefix = f"v{self._vault.current_version}:%"
        rewritten = 0
        failures: list[RekeyFailure] = []
        async with self._session.transaction():
            rows = await self._session.fetch_limited(
                select, (after_id, current_prefix, self._batch_size), limit=self._batch_size
            )
            for row in rows:
                row_id, stored = int(row["id"]), str(row["stored"])
                try:
                    reseal = self._vault.reseal(stored, secret_field, row_id)
                except DecryptionError:
                    failures.append(RekeyFailure(secret_field, row_id))
                    continue
                # Matching the old value means a concurrent write to the same row is kept.
                result = await self._session.execute(update, (reseal, row_id, stored))
                rewritten += result.rowcount
        if len(rows) < self._batch_size:
            return rewritten, -1, failures
        return rewritten, int(rows[-1]["id"]), failures
