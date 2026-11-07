"""Repository for resource_control and its user and team grants."""

import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.db.engine import DatabaseError
from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors

ResourceType = Literal["container", "service", "volume", "secret", "config", "stack", "network"]
AccessLevel = Literal["read", "write"]

# One page of the standing scan. The scan runs to completion, so this bounds a query, not a result.
_SCAN_PAGE = 500
# Per control. The service refuses larger grant sets, so a read never truncates one.
MAX_GRANTS_PER_KIND = 500

_SELECT = (
    "SELECT id, environment_id, resource_type, resource_id, is_public, is_administrators_only, "
    "access_level, owner_user_id, sub_resource_ids, created_at, updated_at FROM resource_control"
)

# The narrowed form binds :ids, a JSON array of the object ids wanted.
# Membership is joined at query time so a removed member loses a team grant on the next read.
_STANDING_HEAD = """
SELECT rc.id AS control_id, rc.resource_id, rc.is_public, rc.is_administrators_only,
       rc.access_level,
       COALESCE(rc.owner_user_id = :user, 0) AS is_owner,
       EXISTS (
           SELECT 1 FROM resource_control_user_access ua
           WHERE ua.resource_control_id = rc.id AND ua.user_id = :user
       ) AS user_granted,
       EXISTS (
           SELECT 1 FROM resource_control_team_access ta
           JOIN team_membership tm ON tm.team_id = ta.team_id
           WHERE ta.resource_control_id = rc.id AND tm.user_id = :user
       ) AS team_granted
FROM resource_control rc
WHERE rc.environment_id = :environment AND rc.resource_type = :type AND rc.id > :position
"""
_STANDING_TAIL = """
ORDER BY rc.id
LIMIT :page_size
"""
_STANDING_SCAN = _STANDING_HEAD + _STANDING_TAIL
_STANDING_SCAN_NARROWED = (
    _STANDING_HEAD + "AND rc.resource_id IN (SELECT value FROM json_each(:ids))" + _STANDING_TAIL
)


def canonical_stack_key(value: str) -> str:
    """Return the form in which two spellings of one stack name compare equal.

    Both sides compare in this form: Compose lower-cases a project name, Swarm keeps its case. The
    unique index and SQL filter use SQLite lower(), which folds ASCII only; stack names are held to
    ASCII on write so the two agree.
    """
    return value.casefold()


def _match_key(resource_type: ResourceType, resource_id: str) -> tuple[str, str]:
    """Return the WHERE term and bound value that find one object's control."""
    if resource_type == "stack":
        return "lower(resource_id) = ?", canonical_stack_key(resource_id)
    return "resource_id = ?", resource_id


@dataclass(frozen=True, slots=True)
class ResourceControl:
    """The authorisation record for one object; sub_resource_ids is a display cache only."""

    id: int
    environment_id: int
    resource_type: ResourceType
    resource_id: str
    is_public: bool
    is_administrators_only: bool
    access_level: AccessLevel
    owner_user_id: int | None
    sub_resource_ids: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ControlGrants:
    """The users and teams a control grants access to."""

    user_ids: tuple[int, ...]
    team_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ControlStanding:
    """What one user's access to one control rests on."""

    control_id: int
    resource_id: str
    is_public: bool
    is_administrators_only: bool
    access_level: AccessLevel
    is_owner: bool
    user_granted: bool
    team_granted: bool


class ResourceControlRepository(ClockedRepository[ResourceControl]):
    """Reads and writes controls, addressed by (environment, type, object id)."""

    def _map_row(self, row: sqlite3.Row) -> ResourceControl:
        return row_as(
            ResourceControl,
            {
                **dict(row),
                "is_public": bool(row["is_public"]),
                "is_administrators_only": bool(row["is_administrators_only"]),
                "sub_resource_ids": tuple(json.loads(row["sub_resource_ids"])),
            },
        )

    async def create(
        self,
        environment_id: int,
        resource_type: ResourceType,
        resource_id: str,
        *,
        is_public: bool,
        is_administrators_only: bool,
        access_level: AccessLevel,
        owner_user_id: int | None,
    ) -> ResourceControl:
        """Insert a control; raise DuplicateError if the object has one, MissingReferenceError."""
        now = self._now()
        with constraints_as_domain_errors("resource control"):
            result = await self._execute(
                "INSERT INTO resource_control (environment_id, resource_type, resource_id, "
                "is_public, is_administrators_only, access_level, owner_user_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    environment_id,
                    resource_type,
                    resource_id,
                    int(is_public),
                    int(is_administrators_only),
                    access_level,
                    owner_user_id,
                    now,
                    now,
                ),
            )
        return await self._created(result, self.get)

    async def get(self, control_id: int) -> ResourceControl | None:
        """Fetch a control by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (control_id,))

    async def get_for(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> ResourceControl | None:
        """Fetch the control over one object; a stack is found whatever the case of its name."""
        term, value = _match_key(resource_type, resource_id)
        return await self._one(
            f"{_SELECT} WHERE environment_id = ? AND resource_type = ? AND {term}",
            (environment_id, resource_type, value),
        )

    async def update(
        self,
        control_id: int,
        *,
        is_public: bool,
        is_administrators_only: bool,
        access_level: AccessLevel,
        owner_user_id: int | None,
    ) -> bool:
        """Replace a control's flags, level and owner; return False if it does not exist."""
        with constraints_as_domain_errors("resource control"):
            result = await self._execute(
                "UPDATE resource_control SET is_public = ?, is_administrators_only = ?, "
                "access_level = ?, owner_user_id = ?, updated_at = ? WHERE id = ?",
                (
                    int(is_public),
                    int(is_administrators_only),
                    access_level,
                    owner_user_id,
                    self._now(),
                    control_id,
                ),
            )
        return result.rowcount > 0

    async def delete(self, control_id: int) -> bool:
        """Delete a control and, by cascade, its grants; return False if it does not exist."""
        result = await self._execute("DELETE FROM resource_control WHERE id = ?", (control_id,))
        return result.rowcount > 0

    async def delete_for(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> bool:
        """Delete the control over one object; return False if it had none."""
        term, value = _match_key(resource_type, resource_id)
        result = await self._execute(
            "DELETE FROM resource_control "
            f"WHERE environment_id = ? AND resource_type = ? AND {term}",
            (environment_id, resource_type, value),
        )
        return result.rowcount > 0

    async def rekey(
        self,
        environment_id: int,
        resource_type: ResourceType,
        resource_id: str,
        new_resource_id: str,
    ) -> bool:
        """Point the control over one object at another id, keeping its owner, flags and grants.

        Return False if the object had no control; DuplicateError if the new id already has one.
        """
        term, value = _match_key(resource_type, resource_id)
        with constraints_as_domain_errors("resource control"):
            result = await self._execute(
                "UPDATE resource_control SET resource_id = ?, updated_at = ? "
                f"WHERE environment_id = ? AND resource_type = ? AND {term}",
                (new_resource_id, self._now(), environment_id, resource_type, value),
            )
        return result.rowcount > 0

    async def copy_to(
        self,
        environment_id: int,
        resource_type: ResourceType,
        resource_id: str,
        new_resource_id: str,
    ) -> bool:
        """Give another id its own copy of an object's control, owner and grants.

        Return False if the object had no control; DuplicateError if the new id already has one.
        Several statements, so the caller opens the transaction.
        """
        source = await self.get_for(environment_id, resource_type, resource_id)
        if source is None:
            return False
        now = self._now()
        with constraints_as_domain_errors("resource control"):
            result = await self._execute(
                "INSERT INTO resource_control (environment_id, resource_type, resource_id, "
                "is_public, is_administrators_only, access_level, owner_user_id, "
                "sub_resource_ids, created_at, updated_at) "
                "SELECT environment_id, resource_type, ?, is_public, is_administrators_only, "
                "access_level, owner_user_id, sub_resource_ids, ?, ? "
                "FROM resource_control WHERE id = ?",
                (new_resource_id, now, now, source.id),
            )
        copied_id = result.last_row_id
        if copied_id is None:
            raise DatabaseError("an insert did not report a row id")
        await self._execute(
            "INSERT INTO resource_control_user_access (resource_control_id, user_id) "
            "SELECT ?, user_id FROM resource_control_user_access WHERE resource_control_id = ?",
            (copied_id, source.id),
        )
        await self._execute(
            "INSERT INTO resource_control_team_access (resource_control_id, team_id) "
            "SELECT ?, team_id FROM resource_control_team_access WHERE resource_control_id = ?",
            (copied_id, source.id),
        )
        return True

    async def replace_grants(
        self, control_id: int, *, user_ids: Collection[int], team_ids: Collection[int]
    ) -> None:
        """Make the grants exactly these sets; raise MissingReferenceError for an unknown id.

        Two statements, so the caller opens the transaction.
        """
        await self._execute(
            "DELETE FROM resource_control_user_access WHERE resource_control_id = ?",
            (control_id,),
        )
        await self._execute(
            "DELETE FROM resource_control_team_access WHERE resource_control_id = ?",
            (control_id,),
        )
        with constraints_as_domain_errors("resource control grant"):
            for user_id in sorted(set(user_ids)):
                await self._execute(
                    "INSERT INTO resource_control_user_access (resource_control_id, user_id) "
                    "VALUES (?, ?)",
                    (control_id, user_id),
                )
            for team_id in sorted(set(team_ids)):
                await self._execute(
                    "INSERT INTO resource_control_team_access (resource_control_id, team_id) "
                    "VALUES (?, ?)",
                    (control_id, team_id),
                )

    async def list_grants(self, control_id: int) -> ControlGrants:
        """Return the users and teams a control grants access to."""
        users = await self._session.fetch_limited(
            "SELECT user_id FROM resource_control_user_access "
            "WHERE resource_control_id = ? ORDER BY user_id LIMIT ?",
            (control_id, MAX_GRANTS_PER_KIND),
            limit=MAX_GRANTS_PER_KIND,
        )
        teams = await self._session.fetch_limited(
            "SELECT team_id FROM resource_control_team_access "
            "WHERE resource_control_id = ? ORDER BY team_id LIMIT ?",
            (control_id, MAX_GRANTS_PER_KIND),
            limit=MAX_GRANTS_PER_KIND,
        )
        return ControlGrants(
            tuple(int(row["user_id"]) for row in users),
            tuple(int(row["team_id"]) for row in teams),
        )

    async def list_granting_team_names(self, control_id: int, user_id: int) -> list[str]:
        """Name the teams through which a user currently holds a grant on a control."""
        rows = await self._session.fetch_limited(
            "SELECT DISTINCT t.name AS name FROM resource_control_team_access ta "
            "JOIN team_membership tm ON tm.team_id = ta.team_id "
            "JOIN team t ON t.id = ta.team_id "
            "WHERE ta.resource_control_id = ? AND tm.user_id = ? ORDER BY t.name LIMIT ?",
            (control_id, user_id, MAX_GRANTS_PER_KIND),
            limit=MAX_GRANTS_PER_KIND,
        )
        return [str(row["name"]) for row in rows]

    async def list_standing(
        self,
        user_id: int,
        environment_id: int,
        resource_type: ResourceType,
        resource_ids: Collection[str] | None = None,
    ) -> list[ControlStanding]:
        """Return the controls of one type in one environment, with the user's standing on each.

        Scans to completion: a truncated list would read as "uncontrolled" for the objects past
        the cut. resource_ids narrows the scan to those objects; the caller then owns the guarantee
        that it asks about no others. It is ignored for a stack, whose controls are always read in
        full.
        """
        bound: dict[str, Any] = {
            "user": user_id,
            "environment": environment_id,
            "type": resource_type,
            "page_size": _SCAN_PAGE,
        }
        if resource_ids is None or resource_type == "stack":
            query = _STANDING_SCAN
        else:
            query = _STANDING_SCAN_NARROWED
            bound["ids"] = json.dumps(sorted(set(resource_ids)))
        standings: list[ControlStanding] = []
        position = 0
        while True:
            rows = await self._session.fetch_limited(
                query,
                {**bound, "position": position},
                limit=_SCAN_PAGE,
            )
            standings.extend(_standing(row) for row in rows)
            if len(rows) < _SCAN_PAGE:
                return standings
            position = int(rows[-1]["control_id"])

    async def set_sub_resource_ids(self, control_id: int, sub_resource_ids: Sequence[str]) -> bool:
        """Replace the display cache of member objects; return False if the control is gone."""
        result = await self._execute(
            "UPDATE resource_control SET sub_resource_ids = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(sub_resource_ids), separators=(",", ":")), self._now(), control_id),
        )
        return result.rowcount > 0


def _standing(row: sqlite3.Row) -> ControlStanding:
    return ControlStanding(
        control_id=int(row["control_id"]),
        resource_id=str(row["resource_id"]),
        is_public=bool(row["is_public"]),
        is_administrators_only=bool(row["is_administrators_only"]),
        access_level=row["access_level"],
        is_owner=bool(row["is_owner"]),
        user_granted=bool(row["user_granted"]),
        team_granted=bool(row["team_granted"]),
    )
