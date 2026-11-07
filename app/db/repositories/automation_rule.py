"""Repository for the automation_rule table."""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import MAX_PAGE_SIZE, Page, validate_limit

_COLUMNS = (
    "id, name, subsystem, environment_id, target_selector, parameters, is_enabled, "
    "paused_until, created_by_user_id, created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM automation_rule"
_ENTITY: Final = "automation rule"


@dataclass(frozen=True, slots=True)
class AutomationRule:
    """A configured automation, as stored; parameters is the canonical JSON text."""

    id: int
    name: str
    subsystem: str
    environment_id: int | None
    target_selector: str
    parameters: str
    is_enabled: bool
    paused_until: str | None
    created_by_user_id: int | None
    created_at: str
    updated_at: str


class AutomationRuleRepository(ClockedRepository[AutomationRule]):
    """Reads and writes rules; the owning subsystem validates parameters before they arrive."""

    def _map_row(self, row: sqlite3.Row) -> AutomationRule:
        return AutomationRule(
            id=row["id"],
            name=row["name"],
            subsystem=row["subsystem"],
            environment_id=row["environment_id"],
            target_selector=row["target_selector"],
            parameters=row["parameters"],
            is_enabled=bool(row["is_enabled"]),
            paused_until=row["paused_until"],
            created_by_user_id=row["created_by_user_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get(self, rule_id: int) -> AutomationRule | None:
        """Fetch a rule by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (rule_id,))

    async def name_taken(
        self, subsystem: str, owner_id: int, name: str, *, excluding: int | None = None
    ) -> bool:
        """Report whether the owner has a rule of the subsystem with this name.

        Names are compared under full Unicode case folding. The unique index folds ASCII only,
        so this walks the owner's rules.
        """
        folded = name.casefold()
        after = 0
        while True:
            rows = await self._session.fetch_limited(
                "SELECT id, name FROM automation_rule WHERE subsystem = ? "
                "AND created_by_user_id = ? AND id > ? ORDER BY id LIMIT ?",
                (subsystem, owner_id, after, MAX_PAGE_SIZE),
                limit=MAX_PAGE_SIZE,
            )
            for row in rows:
                if row["id"] != excluding and str(row["name"]).casefold() == folded:
                    return True
            if len(rows) < MAX_PAGE_SIZE:
                return False
            after = int(rows[-1]["id"])

    async def count_for_owner(self, owner_id: int) -> int:
        """Count the rules a user owns."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM automation_rule WHERE created_by_user_id = ?",
            (owner_id,),
        )
        return 0 if row is None else int(row["total"])

    async def insert(
        self,
        *,
        name: str,
        subsystem: str,
        environment_id: int | None,
        target_selector: str,
        parameters: str,
        is_enabled: bool,
        created_by_user_id: int,
    ) -> AutomationRule:
        """Insert a rule; DuplicateError for a repeated name, MissingReferenceError for a bad id."""
        now = self._now()
        with constraints_as_domain_errors(_ENTITY):
            result = await self._execute(
                "INSERT INTO automation_rule (name, subsystem, environment_id, target_selector, "
                "parameters, is_enabled, created_by_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    subsystem,
                    environment_id,
                    target_selector,
                    parameters,
                    int(is_enabled),
                    created_by_user_id,
                    now,
                    now,
                ),
            )
        return await self._created(result, self.get)

    async def update(
        self, rule_id: int, *, name: str, target_selector: str, parameters: str
    ) -> None:
        """Replace a rule's name, selector and parameters."""
        with constraints_as_domain_errors(_ENTITY):
            await self._execute(
                "UPDATE automation_rule SET name = ?, target_selector = ?, parameters = ?, "
                "updated_at = ? WHERE id = ?",
                (name, target_selector, parameters, self._now(), rule_id),
            )

    async def set_enabled(self, rule_id: int, enabled: bool) -> None:
        """Enable or disable a rule."""
        await self._execute(
            "UPDATE automation_rule SET is_enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), self._now(), rule_id),
        )

    async def set_paused(self, rule_id: int, paused_until: str | None) -> None:
        """Pause a rule until the given time, or clear the pause."""
        await self._execute(
            "UPDATE automation_rule SET paused_until = ?, updated_at = ? WHERE id = ?",
            (paused_until, self._now(), rule_id),
        )

    async def delete(self, rule_id: int) -> bool:
        """Delete a rule and, by cascade, its job and the job's runs; report whether it existed."""
        result = await self._execute("DELETE FROM automation_rule WHERE id = ?", (rule_id,))
        return result.rowcount > 0

    async def for_environment(
        self, environment_id: int, subsystems: Collection[str], *, limit: int
    ) -> list[AutomationRule]:
        """List up to limit rules of the named subsystems in one environment, by id."""
        validate_limit(limit)
        names = sorted(set(subsystems))
        if not names:
            return []
        marks = ", ".join("?" * len(names))
        rows = await self._session.fetch_limited(
            f"{_SELECT} WHERE environment_id = ? AND subsystem IN ({marks}) ORDER BY id LIMIT ?",
            (environment_id, *names, limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]

    @staticmethod
    def _filter(
        subsystem: str | None, owner_id: int | None, environment_id: int | None
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if subsystem is not None:
            clauses.append("subsystem = ?")
            params.append(subsystem)
        if owner_id is not None:
            clauses.append("created_by_user_id = ?")
            params.append(owner_id)
        if environment_id is not None:
            clauses.append("environment_id = ?")
            params.append(environment_id)
        return " AND ".join(clauses) if clauses else "1 = 1", params

    async def page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        subsystem: str | None = None,
        owner_id: int | None = None,
        environment_id: int | None = None,
    ) -> Page[AutomationRule]:
        """Return one page of rules by ascending id, optionally for a subsystem, owner or host."""
        where, params = self._filter(subsystem, owner_id, environment_id)
        return await self._page(
            _SELECT, limit=limit, cursor=cursor, sort_column="id", where=where, params=params
        )

    async def count(
        self,
        *,
        subsystem: str | None = None,
        owner_id: int | None = None,
        environment_id: int | None = None,
    ) -> int:
        """Count the rules page lists for the same filters, across all pages."""
        where, params = self._filter(subsystem, owner_id, environment_id)
        row = await self._session.fetch_one(
            f"SELECT count(*) AS total FROM automation_rule WHERE {where}", tuple(params)
        )
        return 0 if row is None else int(row["total"])
