"""Key version, instance id and canary rows in the setting table, and what secret columns hold."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from app.crypto.envelope import SecretLocation
from app.crypto.registry import SECRET_FIELDS, SecretField
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.encrypted import INSERT_PLACEHOLDER

KEY_VERSION_KEY = "crypto.key_version"
CANARY_KEY = "crypto.canary"
INSTANCE_ID_KEY = "crypto.instance_id"
CANARY_TABLE = "setting"
CANARY_COLUMN = "value"

UNPARSEABLE_REPORT_LIMIT = 10


@dataclass(frozen=True, slots=True)
class CryptoState:
    """The stored key version, instance id, and the canary ciphertext with its row id."""

    key_version: int
    instance_id: str
    canary: str
    canary_row_id: int


@dataclass(frozen=True, slots=True)
class SecretSample:
    """One stored ciphertext, with where it lives."""

    field: SecretField
    row_id: int
    token: str


@dataclass(frozen=True, slots=True)
class UnparseableSecret:
    """A populated secret column whose value is not shaped like a ciphertext token."""

    field: SecretField
    row_id: int


def canary_location(row_id: int) -> SecretLocation:
    """Return the authenticated location of the canary value."""
    return SecretLocation(CANARY_TABLE, CANARY_COLUMN, row_id)


def version_expression(column: str) -> str:
    """Return the SQL yielding a value's key version, NULL when it is not shaped like a token."""
    # Approximates the token shape in app.crypto.envelope; decryption is the real check.
    prefix = f"substr({column}, 2, instr({column}, ':') - 2)"
    return (
        f"CASE WHEN {column} GLOB 'v[1-9]*:?*:?*' "
        f"AND {column} NOT GLOB '*[^A-Za-z0-9+/=:]*' "
        f"AND {column} NOT GLOB '*:*:*:*' "
        f"AND length({prefix}) <= 9 AND {prefix} NOT GLOB '*[^0-9]*' "
        f"THEN CAST({prefix} AS INTEGER) END"
    )


def count_by_version_sql(field: SecretField) -> str:
    """Return the query counting a secret column's well-formed values per key version."""
    return (
        "SELECT version, COUNT(*) AS total FROM ("
        f"SELECT {version_expression(field.column)} AS version FROM {field.table} "
        f"WHERE {field.column} IS NOT NULL) WHERE version IS NOT NULL GROUP BY version"
    )


class CryptoStateRepository(ClockedRepository[CryptoState]):
    """Reads and writes the rows that record which key version the database is at."""

    def _map_row(self, row: sqlite3.Row) -> CryptoState:
        return CryptoState(
            int(row["key_version"]),
            str(row["instance_id"]),
            row["canary"],
            int(row["canary_row_id"]),
        )

    async def get(self) -> CryptoState | None:
        """Return the stored state, or None when any of its three rows is missing."""
        return await self._one(
            "SELECT v.value AS key_version, i.value AS instance_id, c.value AS canary, "
            "c.id AS canary_row_id "
            "FROM setting AS v JOIN setting AS c ON c.key = ? JOIN setting AS i ON i.key = ? "
            "WHERE v.key = ?",
            (CANARY_KEY, INSTANCE_ID_KEY, KEY_VERSION_KEY),
        )

    async def write(
        self, key_version: int, instance_id: str, seal: Callable[[SecretLocation], str]
    ) -> None:
        """Store the key version, instance id and a canary sealed by seal, atomically.

        The canary's row id is part of its authenticated data, so the row is created first and
        sealed in the same transaction.
        """
        now = self._now()
        async with self._session.transaction():
            for key, value_type, value in (
                (KEY_VERSION_KEY, "integer", str(key_version)),
                (INSTANCE_ID_KEY, "string", instance_id),
            ):
                await self._session.execute(
                    "INSERT INTO setting (key, value_type, value, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, value_type, value, now),
                )
            await self._session.execute(
                "INSERT INTO setting (key, value_type, value, updated_at) "
                "VALUES (?, 'string', ?, ?) "
                "ON CONFLICT (key) DO NOTHING",
                (CANARY_KEY, INSERT_PLACEHOLDER, now),
            )
            row = await self._session.fetch_one(
                "SELECT id FROM setting WHERE key = ?", (CANARY_KEY,)
            )
            if row is None:
                raise RuntimeError("the canary row could not be read back")
            row_id = int(row["id"])
            await self._session.execute(
                "UPDATE setting SET value = ?, updated_at = ? WHERE id = ?",
                (seal(canary_location(row_id)), now, row_id),
            )

    async def has_secret_values(self) -> bool:
        """Report whether any secret column holds a value, of any shape."""
        for field in SECRET_FIELDS:
            row = await self._session.fetch_one(
                f"SELECT 1 AS present FROM {field.table} WHERE {field.column} IS NOT NULL LIMIT 1"
            )
            if row is not None:
                return True
        return False

    async def has_crypto_settings(self) -> bool:
        """Report whether any crypto.* setting row exists, complete or not."""
        row = await self._session.fetch_one(
            "SELECT 1 AS present FROM setting WHERE key GLOB 'crypto.*' LIMIT 1"
        )
        return row is not None

    async def count_by_version(self) -> dict[int, int]:
        """Return how many well-formed secret values exist at each key version."""
        totals: dict[int, int] = {}
        for field in SECRET_FIELDS:
            rows = await self._session.fetch_limited(count_by_version_sql(field), (), limit=10_000)
            for row in rows:
                totals[int(row["version"])] = totals.get(int(row["version"]), 0) + int(row["total"])
        return totals

    async def count_by_field_and_version(self) -> dict[tuple[str, str], dict[int, int]]:
        """Return well-formed value counts per key version for every secret column."""
        totals: dict[tuple[str, str], dict[int, int]] = {}
        for field in SECRET_FIELDS:
            rows = await self._session.fetch_limited(count_by_version_sql(field), (), limit=10_000)
            totals[(field.table, field.column)] = {
                int(row["version"]): int(row["total"]) for row in rows
            }
        return totals

    async def find_unparseable(self) -> list[UnparseableSecret]:
        """Return a bounded number per column of populated values that are not ciphertext."""
        found: list[UnparseableSecret] = []
        for field in SECRET_FIELDS:
            rows = await self._session.fetch_limited(
                f"SELECT id FROM {field.table} WHERE {field.column} IS NOT NULL "
                f"AND {version_expression(field.column)} IS NULL ORDER BY id LIMIT ?",
                (UNPARSEABLE_REPORT_LIMIT,),
                limit=UNPARSEABLE_REPORT_LIMIT,
            )
            found.extend(UnparseableSecret(field, int(row["id"])) for row in rows)
        return found

    async def sample_at_version(self, version: int) -> list[SecretSample]:
        """Return the lowest-id value at a key version from each secret column that has one."""
        samples: list[SecretSample] = []
        for field in SECRET_FIELDS:
            row = await self._session.fetch_one(
                f"SELECT id, {field.column} AS stored FROM {field.table} "
                f"WHERE {field.column} LIKE ? ORDER BY id LIMIT 1",
                (f"v{version}:%",),
            )
            if row is not None:
                samples.append(SecretSample(field, int(row["id"]), str(row["stored"])))
        return samples
