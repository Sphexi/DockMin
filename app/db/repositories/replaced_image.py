"""Repository for replaced_image: images a rule's updates replaced, waiting to be pruned."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository, format_timestamp
from app.db.repositories.errors import constraints_as_domain_errors

ReplacedTargetType = Literal["container", "stack"]

_COLUMNS = "rule_id, rule_created_at, environment_id, image_id, replaced_at, target_type, target_id"


@dataclass(frozen=True, slots=True)
class ReplacedImage:
    """An image a rule's update replaced and the container or stack it was replaced on."""

    rule_id: int
    rule_created_at: str
    environment_id: int
    image_id: str
    replaced_at: str
    target_type: ReplacedTargetType
    target_id: str


@dataclass(frozen=True, slots=True)
class WaitingImages:
    """How many records a rule holds and when the oldest was written."""

    count: int
    oldest_replaced_at: str | None


class ReplacedImageRepository(ClockedRepository[ReplacedImage]):
    """Reads and writes records; every call names the rule's creation time.

    Rule ids are reused after a delete, so a row stored under another creation time is replaced
    by a write and never returned by a read.
    """

    def _map_row(self, row: sqlite3.Row) -> ReplacedImage:
        return row_as(ReplacedImage, dict(row))

    async def add_replaced(
        self,
        rule: AutomationRule,
        environment_id: int,
        image_id: str,
        target_type: ReplacedTargetType,
        target_id: str,
    ) -> None:
        """Store the image as replaced now; a record for the same image is overwritten.

        Raises MissingReferenceError when the rule or the environment does not exist.
        """
        with constraints_as_domain_errors("replaced image"):
            await self._execute(
                f"INSERT INTO replaced_image ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (rule_id, environment_id, image_id) DO UPDATE SET "
                "rule_created_at = excluded.rule_created_at, "
                "replaced_at = excluded.replaced_at, target_type = excluded.target_type, "
                "target_id = excluded.target_id",
                (
                    rule.id,
                    rule.created_at,
                    environment_id,
                    image_id,
                    self._now(),
                    target_type,
                    target_id,
                ),
            )

    async def due(
        self,
        rule: AutomationRule,
        environment_id: int,
        now: datetime,
        grace: timedelta,
        limit: int,
    ) -> list[ReplacedImage]:
        """Return up to `limit` of the rule's records replaced at least `grace` before `now`."""
        rows = await self._session.fetch_limited(
            f"SELECT {_COLUMNS} FROM replaced_image "
            "WHERE rule_id = ? AND rule_created_at = ? AND environment_id = ? AND replaced_at <= ? "
            "ORDER BY replaced_at, image_id LIMIT ?",
            (
                rule.id,
                rule.created_at,
                environment_id,
                format_timestamp(now - grace),
                limit,
            ),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]

    async def delete(self, rule: AutomationRule, environment_id: int, image_id: str) -> bool:
        """Delete one record of the rule; report whether there was one."""
        result = await self._execute(
            "DELETE FROM replaced_image "
            "WHERE rule_id = ? AND rule_created_at = ? AND environment_id = ? AND image_id = ?",
            (rule.id, rule.created_at, environment_id, image_id),
        )
        return result.rowcount > 0

    async def delete_for_rule(self, rule_id: int) -> int:
        """Delete every record stored under the rule id, whatever its creation time."""
        result = await self._execute("DELETE FROM replaced_image WHERE rule_id = ?", (rule_id,))
        return result.rowcount

    async def drop_stale(self, rule: AutomationRule) -> int:
        """Delete the records stored under the rule id by another creation of the rule."""
        result = await self._execute(
            "DELETE FROM replaced_image WHERE rule_id = ? AND rule_created_at != ?",
            (rule.id, rule.created_at),
        )
        return result.rowcount

    async def waiting(self, rule: AutomationRule) -> WaitingImages:
        """Count the rule's records and return the oldest write time."""
        row = await self._session.fetch_one(
            "SELECT COUNT(*) AS waiting, MIN(replaced_at) AS oldest FROM replaced_image "
            "WHERE rule_id = ? AND rule_created_at = ?",
            (rule.id, rule.created_at),
        )
        if row is None:
            return WaitingImages(0, None)
        return WaitingImages(int(row["waiting"]), row["oldest"])
