"""Grants of environments and groups to users and teams, and who may see which environment."""

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.db.repositories.base import Repository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.visibility_filter import SqlFragment

MAX_GRANT_LISTING = 500

type GrantScope = Literal["environment", "group"]

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
# Aliases the predicate uses internally; an outer query using one would capture a caller's column.
_INNER_ALIASES = frozenset({"eua", "eta", "gua", "gta", "tm"})


@dataclass(frozen=True, slots=True)
class _Tables:
    users: str
    teams: str
    key: str


_SCOPES: dict[GrantScope, _Tables] = {
    "environment": _Tables("environment_user_access", "environment_team_access", "environment_id"),
    "group": _Tables(
        "environment_group_user_access", "environment_group_team_access", "environment_group_id"
    ),
}


def environment_visible_clause(
    user_id: int, environment_column: str, group_column: str
) -> SqlFragment:
    """Build the expression that is true for environments a standard user holds a grant on.

    Admits an environment granted to the user, to a team they belong to, or whose group is
    granted either way; membership is read inside the statement. Both columns must name their
    table, as in e.id, or the subqueries would capture them. The four params are the user id.
    """
    for name in (environment_column, group_column):
        if _COLUMN.fullmatch(name) is None:
            raise ValueError("column must be a table-qualified column name, as in e.id")
        if name.split(".", 1)[0] in _INNER_ALIASES:
            raise ValueError("column must not use an alias the predicate reserves")
    clause = (
        f"({environment_column} IN ("
        "SELECT eua.environment_id FROM environment_user_access eua WHERE eua.user_id = ? "
        "UNION ALL SELECT eta.environment_id FROM environment_team_access eta "
        "JOIN team_membership tm ON tm.team_id = eta.team_id WHERE tm.user_id = ?) "
        f"OR {group_column} IN ("
        "SELECT gua.environment_group_id FROM environment_group_user_access gua "
        "WHERE gua.user_id = ? "
        "UNION ALL SELECT gta.environment_group_id FROM environment_group_team_access gta "
        "JOIN team_membership tm ON tm.team_id = gta.team_id WHERE tm.user_id = ?))"
    )
    return SqlFragment(clause, (user_id, user_id, user_id, user_id))


@dataclass(frozen=True, slots=True)
class EnvironmentGate:
    """What the visibility check learned about an environment the caller may see."""

    is_local: bool


@dataclass(frozen=True, slots=True)
class GrantedUser:
    """A user holding a grant."""

    user_id: int
    username: str


@dataclass(frozen=True, slots=True)
class GrantedTeam:
    """A team holding a grant."""

    team_id: int
    name: str


class EnvironmentAccessRepository(Repository[GrantedUser]):
    """Reads and writes environment and group grants, and answers the visibility question."""

    def _map_row(self, row: sqlite3.Row) -> GrantedUser:
        return GrantedUser(int(row["user_id"]), str(row["username"]))

    async def gate(self, user_id: int, environment_id: int) -> EnvironmentGate | None:
        """Return the environment's facts if a standard user holds a grant on it, else None."""
        fragment = environment_visible_clause(user_id, "e.id", "e.environment_group_id")
        row = await self._session.fetch_one(
            f"SELECT e.connection_kind AS connection_kind FROM environment e "
            f"WHERE e.id = ? AND {fragment.clause}",
            (environment_id, *fragment.params),
        )
        return (
            None
            if row is None
            else EnvironmentGate(is_local=row["connection_kind"] == "unix_socket")
        )

    async def add_user(self, scope: GrantScope, target_id: int, user_id: int) -> None:
        """Grant to a user; DuplicateError if held, MissingReferenceError if either is gone."""
        tables = _SCOPES[scope]
        with constraints_as_domain_errors("access grant"):
            await self._execute(
                f"INSERT INTO {tables.users} ({tables.key}, user_id) VALUES (?, ?)",
                (target_id, user_id),
            )

    async def add_team(self, scope: GrantScope, target_id: int, team_id: int) -> None:
        """Grant to a team; DuplicateError if held, MissingReferenceError if either is gone."""
        tables = _SCOPES[scope]
        with constraints_as_domain_errors("access grant"):
            await self._execute(
                f"INSERT INTO {tables.teams} ({tables.key}, team_id) VALUES (?, ?)",
                (target_id, team_id),
            )

    async def remove_user(self, scope: GrantScope, target_id: int, user_id: int) -> bool:
        """Withdraw a user's grant; False if there was none."""
        tables = _SCOPES[scope]
        result = await self._execute(
            f"DELETE FROM {tables.users} WHERE {tables.key} = ? AND user_id = ?",
            (target_id, user_id),
        )
        return result.rowcount > 0

    async def remove_team(self, scope: GrantScope, target_id: int, team_id: int) -> bool:
        """Withdraw a team's grant; False if there was none."""
        tables = _SCOPES[scope]
        result = await self._execute(
            f"DELETE FROM {tables.teams} WHERE {tables.key} = ? AND team_id = ?",
            (target_id, team_id),
        )
        return result.rowcount > 0

    async def list_users(
        self, scope: GrantScope, target_id: int, *, limit: int
    ) -> list[GrantedUser]:
        """List users granted directly, by username; at most limit."""
        tables = _SCOPES[scope]
        rows = await self._session.fetch_limited(
            f"SELECT a.user_id AS user_id, u.username AS username FROM {tables.users} a "
            f"JOIN user u ON u.id = a.user_id WHERE a.{tables.key} = ? "
            "ORDER BY u.username COLLATE NOCASE, u.id LIMIT ?",
            (target_id, limit),
            limit=limit,
        )
        return [self._map_row(row) for row in rows]

    async def list_teams(
        self, scope: GrantScope, target_id: int, *, limit: int
    ) -> list[GrantedTeam]:
        """List teams granted directly, by name; at most limit."""
        tables = _SCOPES[scope]
        rows = await self._session.fetch_limited(
            f"SELECT a.team_id AS team_id, t.name AS name FROM {tables.teams} a "
            f"JOIN team t ON t.id = a.team_id WHERE a.{tables.key} = ? "
            "ORDER BY t.name COLLATE NOCASE, t.id LIMIT ?",
            (target_id, limit),
            limit=limit,
        )
        return [GrantedTeam(int(row["team_id"]), str(row["name"])) for row in rows]

    async def count_grants(self, scope: GrantScope, target_id: int) -> tuple[int, int]:
        """Return how many users and how many teams are granted directly."""
        tables = _SCOPES[scope]
        row = await self._session.fetch_one(
            f"SELECT (SELECT count(*) FROM {tables.users} WHERE {tables.key} = ?) AS users, "
            f"(SELECT count(*) FROM {tables.teams} WHERE {tables.key} = ?) AS teams",
            (target_id, target_id),
        )
        assert row is not None
        return int(row["users"]), int(row["teams"])

    async def count_standard_users_with_access(self, environment_id: int) -> int:
        """Count enabled standard users who can reach the environment by any grant."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM user u "
            "WHERE u.role = 'standard' AND u.is_enabled = 1 AND ("
            "u.id IN (SELECT eua.user_id FROM environment_user_access eua "
            "WHERE eua.environment_id = ?) "
            "OR u.id IN (SELECT tm.user_id FROM environment_team_access eta "
            "JOIN team_membership tm ON tm.team_id = eta.team_id "
            "WHERE eta.environment_id = ?) "
            "OR u.id IN (SELECT gua.user_id FROM environment_group_user_access gua "
            "JOIN environment e ON e.environment_group_id = gua.environment_group_id "
            "WHERE e.id = ?) "
            "OR u.id IN (SELECT tm.user_id FROM environment_group_team_access gta "
            "JOIN environment e ON e.environment_group_id = gta.environment_group_id "
            "JOIN team_membership tm ON tm.team_id = gta.team_id "
            "WHERE e.id = ?))",
            (environment_id, environment_id, environment_id, environment_id),
        )
        assert row is not None
        return int(row["total"])

    async def count_standard_users_with_group_access(self, group_id: int) -> int:
        """Count enabled standard users who reach the group's environments by a group grant."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM user u "
            "WHERE u.role = 'standard' AND u.is_enabled = 1 AND ("
            "u.id IN (SELECT gua.user_id FROM environment_group_user_access gua "
            "WHERE gua.environment_group_id = ?) "
            "OR u.id IN (SELECT tm.user_id FROM environment_group_team_access gta "
            "JOIN team_membership tm ON tm.team_id = gta.team_id "
            "WHERE gta.environment_group_id = ?))",
            (group_id, group_id),
        )
        assert row is not None
        return int(row["total"])
