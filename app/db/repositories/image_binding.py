"""Repository for image_credential_binding: the only writer of the table."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from app.db.repositories.base import Repository
from app.db.repositories.clocked import format_timestamp

BindingStateName = Literal["bound", "credential_missing"]
MissingReason = Literal["deleted", "address_changed"]

MAX_LOOKUP_REFERENCES: Final = 400
MAX_AFFECTED_LISTING: Final = 500
SCAN_PAGE: Final = 500

_SELECT = (
    "SELECT b.id AS id, b.environment_id AS environment_id, b.image_reference AS image_reference, "
    "b.registry_credential_id AS registry_credential_id, rc.name AS credential_name, "
    "rc.url AS credential_url, b.missing_credential_id AS missing_credential_id, "
    "b.missing_credential_name AS missing_credential_name, b.missing_reason AS missing_reason, "
    "b.observed_digest AS observed_digest, b.state AS state, b.bound_at AS bound_at, "
    "b.updated_at AS updated_at, b.credential_missing_at AS credential_missing_at, "
    "rc.provider AS credential_provider "
    "FROM image_credential_binding b "
    "LEFT JOIN registry_credential rc ON rc.id = b.registry_credential_id"
)


@dataclass(frozen=True, slots=True)
class StoredBinding:
    """One binding as stored, with the name, address and provider of the set it names, if any.

    A bound row names a set. A credential_missing row records the id and name of the set it lost.
    """

    id: int
    environment_id: int
    image_reference: str
    registry_credential_id: int | None
    credential_name: str | None
    credential_url: str | None
    missing_credential_id: int | None
    missing_credential_name: str | None
    missing_reason: MissingReason | None
    observed_digest: str | None
    state: BindingStateName
    bound_at: str
    updated_at: str
    credential_missing_at: str | None
    credential_provider: str | None = None


@dataclass(frozen=True, slots=True)
class AffectedBinding:
    """A binding of one credential set, named with its environment."""

    id: int
    environment_id: int
    environment_name: str
    image_reference: str


@dataclass(frozen=True, slots=True)
class BindingRef:
    """The identity of a binding: its id and what it binds."""

    id: int
    environment_id: int
    image_reference: str


@dataclass(frozen=True, slots=True)
class EnvironmentBindingCount:
    """How many bindings of one credential set an environment holds."""

    environment_id: int
    environment_name: str
    count: int


class ImageBindingRepository(Repository[StoredBinding]):
    """Reads and writes image-to-credential bindings, keyed by environment and reference."""

    def _map_row(self, row: sqlite3.Row) -> StoredBinding:
        return StoredBinding(
            id=int(row["id"]),
            environment_id=int(row["environment_id"]),
            image_reference=str(row["image_reference"]),
            registry_credential_id=row["registry_credential_id"],
            credential_name=row["credential_name"],
            credential_url=row["credential_url"],
            missing_credential_id=row["missing_credential_id"],
            missing_credential_name=row["missing_credential_name"],
            missing_reason=cast(MissingReason | None, row["missing_reason"]),
            observed_digest=row["observed_digest"],
            state=cast(BindingStateName, row["state"]),
            bound_at=str(row["bound_at"]),
            updated_at=str(row["updated_at"]),
            credential_missing_at=row["credential_missing_at"],
            credential_provider=row["credential_provider"],
        )

    async def get(self, environment_id: int, image_reference: str) -> StoredBinding | None:
        """Fetch the binding of one reference on one environment."""
        return await self._one(
            f"{_SELECT} WHERE b.environment_id = ? AND b.image_reference = ?",
            (environment_id, image_reference),
        )

    async def get_by_id(self, binding_id: int) -> StoredBinding | None:
        """Fetch a binding by its id."""
        return await self._one(f"{_SELECT} WHERE b.id = ?", (binding_id,))

    async def for_references(
        self, environment_id: int, references: Sequence[str]
    ) -> dict[str, StoredBinding]:
        """Fetch the bindings that exist among the references, keyed by reference."""
        found: dict[str, StoredBinding] = {}
        distinct = list(dict.fromkeys(references))
        for start in range(0, len(distinct), MAX_LOOKUP_REFERENCES):
            chunk = distinct[start : start + MAX_LOOKUP_REFERENCES]
            marks = ", ".join("?" for _ in chunk)
            rows = await self._session.fetch_limited(
                f"{_SELECT} WHERE b.environment_id = ? AND b.image_reference IN ({marks})",
                (environment_id, *chunk),
                limit=len(chunk),
            )
            for row in rows:
                binding = self._map_row(row)
                found[binding.image_reference] = binding
        return found

    async def scan_environment(self, environment_id: int, *, after_id: int) -> list[StoredBinding]:
        """Return the next page of an environment's bindings, in id order, after the given id."""
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE b.environment_id = ? AND b.id > ? ORDER BY b.id LIMIT ?",
            (environment_id, after_id, SCAN_PAGE),
            limit=SCAN_PAGE,
        )
        return [self._map_row(row) for row in rows]

    async def credential_exists(self, credential_id: int) -> bool:
        """Report whether a credential set with this id exists."""
        row = await self._session.fetch_one(
            "SELECT 1 AS present FROM registry_credential WHERE id = ?", (credential_id,)
        )
        return row is not None

    async def has_granted_set_at(self, environment_id: int, address: str) -> bool:
        """Report whether any credential set at the address is granted to the environment."""
        row = await self._session.fetch_one(
            "SELECT 1 AS present FROM registry_credential rc "
            "JOIN registry_environment_access rea ON rea.registry_credential_id = rc.id "
            "WHERE rc.url = ? AND rea.environment_id = ? LIMIT 1",
            (address, environment_id),
        )
        return row is not None

    async def count_for_credential(self, credential_id: int) -> int:
        """Count the bindings that name the set."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM image_credential_binding "
            "WHERE registry_credential_id = ?",
            (credential_id,),
        )
        assert row is not None
        return int(row["total"])

    async def list_affected(
        self, credential_id: int, *, limit: int = MAX_AFFECTED_LISTING
    ) -> list[AffectedBinding]:
        """List the set's bindings by environment name, then reference; at most `limit`."""
        rows = await self._session.fetch_limited(
            "SELECT b.id AS id, e.id AS environment_id, e.name AS environment_name, "
            "b.image_reference AS image_reference "
            "FROM environment e CROSS JOIN image_credential_binding b "
            "ON b.environment_id = e.id AND b.registry_credential_id = ? "
            "ORDER BY e.name, b.image_reference LIMIT ?",
            (credential_id, limit),
            limit=limit,
        )
        return [
            AffectedBinding(
                int(row["id"]),
                int(row["environment_id"]),
                str(row["environment_name"]),
                str(row["image_reference"]),
            )
            for row in rows
        ]

    async def counts_by_environment(
        self, credential_id: int, *, limit: int = MAX_AFFECTED_LISTING
    ) -> list[EnvironmentBindingCount]:
        """Count the set's bindings per environment, by environment name."""
        rows = await self._session.fetch_limited(
            "SELECT e.id AS environment_id, e.name AS environment_name, count(*) AS total "
            "FROM environment e CROSS JOIN image_credential_binding b "
            "ON b.environment_id = e.id AND b.registry_credential_id = ? "
            "GROUP BY e.id ORDER BY e.name LIMIT ?",
            (credential_id, limit),
            limit=limit,
        )
        return [
            EnvironmentBindingCount(
                int(row["environment_id"]), str(row["environment_name"]), int(row["total"])
            )
            for row in rows
        ]

    async def refs_for_credential(self, credential_id: int, *, after: int) -> list[BindingRef]:
        """Return the next page of the set's bindings in id order, after the given id."""
        rows = await self._session.fetch_limited(
            "SELECT id, environment_id, image_reference FROM image_credential_binding "
            "WHERE registry_credential_id = ? AND id > ? ORDER BY id LIMIT ?",
            (credential_id, after, SCAN_PAGE),
            limit=SCAN_PAGE,
        )
        return [
            BindingRef(int(row["id"]), int(row["environment_id"]), str(row["image_reference"]))
            for row in rows
        ]

    async def insert(
        self,
        *,
        environment_id: int,
        image_reference: str,
        credential_id: int,
        observed_digest: str | None,
        now: datetime,
    ) -> None:
        """Create a bound row naming the set."""
        stamp = format_timestamp(now)
        await self._execute(
            "INSERT INTO image_credential_binding (environment_id, image_reference, "
            "registry_credential_id, observed_digest, state, bound_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'bound', ?, ?)",
            (environment_id, image_reference, credential_id, observed_digest, stamp, stamp),
        )

    async def update_digest(
        self, binding_id: int, *, observed_digest: str | None, now: datetime
    ) -> None:
        """Record the digest last seen for a bound row."""
        await self._execute(
            "UPDATE image_credential_binding SET observed_digest = ?, updated_at = ? WHERE id = ?",
            (observed_digest, format_timestamp(now), binding_id),
        )

    async def rebind(
        self,
        binding_id: int,
        *,
        credential_id: int,
        observed_digest: str | None,
        now: datetime,
    ) -> None:
        """Point a row at a set and return it to bound; the repair path."""
        await self._execute(
            "UPDATE image_credential_binding SET registry_credential_id = ?, "
            "observed_digest = ?, state = 'bound', missing_credential_id = NULL, "
            "missing_credential_name = NULL, missing_reason = NULL, "
            "credential_missing_at = NULL, updated_at = ? WHERE id = ?",
            (credential_id, observed_digest, format_timestamp(now), binding_id),
        )

    async def mark_missing_for_credential(
        self, credential_id: int, *, name: str, reason: MissingReason, now: datetime
    ) -> int:
        """Move every binding of a set to credential_missing and return how many moved."""
        stamp = format_timestamp(now)
        result = await self._execute(
            "UPDATE image_credential_binding SET state = 'credential_missing', "
            "registry_credential_id = NULL, missing_credential_id = ?, "
            "missing_credential_name = ?, missing_reason = ?, credential_missing_at = ?, "
            "updated_at = ? WHERE registry_credential_id = ?",
            (credential_id, name, reason, stamp, stamp, credential_id),
        )
        return result.rowcount

    async def delete_if_same(
        self, binding_id: int, *, environment_id: int, image_reference: str
    ) -> bool:
        """Delete a binding if it still has this environment and reference; False otherwise."""
        result = await self._execute(
            "DELETE FROM image_credential_binding "
            "WHERE id = ? AND environment_id = ? AND image_reference = ?",
            (binding_id, environment_id, image_reference),
        )
        return result.rowcount > 0
