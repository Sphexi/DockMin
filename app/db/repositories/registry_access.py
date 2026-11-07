"""Who may use a credential set: environment grants, user and team grants, and the offer query."""

import re
import sqlite3
from dataclasses import dataclass
from typing import cast

from app.db.repositories.base import Repository
from app.db.repositories.environment_access import GrantedTeam, GrantedUser
from app.db.repositories.errors import RepositoryError, constraints_as_domain_errors
from app.db.repositories.visibility_filter import SqlFragment
from app.registry_client.providers import ProviderKind, SecretKind

MAX_OFFERED_SETS = 200
MAX_GRANTED_ENVIRONMENTS = 500

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
# Aliases the predicate uses internally; an outer query using one would capture a caller's column.
_INNER_ALIASES = frozenset({"rua", "rta", "tm"})
_OFFERED = (
    "SELECT rc.id AS id, rc.name AS name, rc.provider AS provider, rc.url AS url, "
    "rc.authentication_enabled AS authentication_enabled, rc.verify_tls AS verify_tls, "
    "rc.secret_kind AS secret_kind, rc.is_default_for_host AS is_default_for_host "
    "FROM registry_credential rc "
    "JOIN registry_environment_access rea ON rea.registry_credential_id = rc.id"
)


def credential_usable_clause(user_id: int, credential_column: str) -> SqlFragment:
    """Build the expression that is true for credential sets a standard user holds a grant on.

    Admits a set granted to the user or to a team they belong to; membership is read inside the
    statement. The column must name its table, as in rc.id, or the subqueries would capture it.
    The two params are the user id.
    """
    if _COLUMN.fullmatch(credential_column) is None:
        raise ValueError("column must be a table-qualified column name, as in rc.id")
    if credential_column.split(".", 1)[0] in _INNER_ALIASES:
        raise ValueError("column must not use an alias the predicate reserves")
    clause = (
        f"({credential_column} IN ("
        "SELECT rua.registry_credential_id FROM registry_credential_user_access rua "
        "WHERE rua.user_id = ? "
        "UNION ALL SELECT rta.registry_credential_id FROM registry_credential_team_access rta "
        "JOIN team_membership tm ON tm.team_id = rta.team_id WHERE tm.user_id = ?))"
    )
    return SqlFragment(clause, (user_id, user_id))


class OfferedSetsOverLimit(RepositoryError):
    """More sets match than the limit; the list is refused, not cut."""


@dataclass(frozen=True, slots=True)
class GrantedEnvironment:
    """An environment a credential set is granted to."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class OfferedCredentialSet:
    """A credential set offered for a pull; carries no secret."""

    id: int
    name: str
    provider: ProviderKind
    url: str
    authentication_enabled: bool
    verify_tls: bool
    secret_kind: SecretKind
    is_default_for_host: bool


class RegistryAccessRepository(Repository[OfferedCredentialSet]):
    """Reads and writes the grants on credential sets, and answers who may use one where."""

    def _map_row(self, row: sqlite3.Row) -> OfferedCredentialSet:
        return OfferedCredentialSet(
            id=int(row["id"]),
            name=str(row["name"]),
            provider=cast(ProviderKind, row["provider"]),
            url=str(row["url"]),
            authentication_enabled=bool(row["authentication_enabled"]),
            verify_tls=bool(row["verify_tls"]),
            secret_kind=cast(SecretKind, row["secret_kind"]),
            is_default_for_host=bool(row["is_default_for_host"]),
        )

    async def eligible_for_pull(
        self,
        environment_id: int,
        *,
        user_id: int | None,
        limit: int = MAX_OFFERED_SETS,
        url: str | None = None,
    ) -> list[OfferedCredentialSet]:
        """List the sets that may be used for a pull on an environment, by name.

        A set must be granted to the environment. With a user id, the user must also hold a user
        or team grant on it; with None, the caller is an administrator and the environment grant
        is enough. With `url` only sets at exactly that address are listed, and more than `limit`
        of them raises OfferedSetsOverLimit; without it the list is cut at `limit`.
        """
        conditions, params = self._eligibility(environment_id, user_id)
        if url is not None:
            conditions.append("rc.url = ?")
            params.append(url)
        fetch = limit + 1 if url is not None else limit
        rows = await self._session.fetch_limited(
            f"{_OFFERED} WHERE {' AND '.join(conditions)} ORDER BY rc.name, rc.id LIMIT ?",
            [*params, fetch],
            limit=fetch,
        )
        if url is not None and len(rows) > limit:
            raise OfferedSetsOverLimit("too many credential sets at this address")
        return [self._map_row(row) for row in rows]

    async def offered_set(
        self, environment_id: int, credential_id: int, *, user_id: int | None
    ) -> OfferedCredentialSet | None:
        """Return one set if it may be used for a pull on the environment, by the same rule."""
        conditions, params = self._eligibility(environment_id, user_id)
        conditions.append("rc.id = ?")
        params.append(credential_id)
        row = await self._session.fetch_one(f"{_OFFERED} WHERE {' AND '.join(conditions)}", params)
        return None if row is None else self._map_row(row)

    @staticmethod
    def _eligibility(environment_id: int, user_id: int | None) -> tuple[list[str], list[int | str]]:
        conditions = ["rea.environment_id = ?"]
        params: list[int | str] = [environment_id]
        if user_id is not None:
            fragment = credential_usable_clause(user_id, "rc.id")
            conditions.append(fragment.clause)
            params.extend(int(value) for value in fragment.params)
        return conditions, params

    async def is_granted_to_environment(self, credential_id: int, environment_id: int) -> bool:
        """Report whether the set is granted to the environment."""
        row = await self._session.fetch_one(
            "SELECT 1 AS present FROM registry_environment_access "
            "WHERE registry_credential_id = ? AND environment_id = ?",
            (credential_id, environment_id),
        )
        return row is not None

    async def user_may_use(self, credential_id: int, user_id: int) -> bool:
        """Report whether a user holds a user grant, or a grant through a team, on the set."""
        fragment = credential_usable_clause(user_id, "rc.id")
        row = await self._session.fetch_one(
            "SELECT 1 AS present FROM registry_credential rc "
            f"WHERE rc.id = ? AND {fragment.clause}",
            (credential_id, *fragment.params),
        )
        return row is not None

    async def add_environment(self, credential_id: int, environment_id: int) -> None:
        """Grant to an environment; DuplicateError if held, MissingReferenceError if one is gone."""
        with constraints_as_domain_errors("environment grant"):
            await self._execute(
                "INSERT INTO registry_environment_access (registry_credential_id, environment_id) "
                "VALUES (?, ?)",
                (credential_id, environment_id),
            )

    async def remove_environment(self, credential_id: int, environment_id: int) -> bool:
        """Withdraw an environment's grant; False if there was none."""
        result = await self._execute(
            "DELETE FROM registry_environment_access "
            "WHERE registry_credential_id = ? AND environment_id = ?",
            (credential_id, environment_id),
        )
        return result.rowcount > 0

    async def add_user(self, credential_id: int, user_id: int) -> None:
        """Grant to a user; DuplicateError if held, MissingReferenceError if either is gone."""
        with constraints_as_domain_errors("access grant"):
            await self._execute(
                "INSERT INTO registry_credential_user_access (registry_credential_id, user_id) "
                "VALUES (?, ?)",
                (credential_id, user_id),
            )

    async def add_team(self, credential_id: int, team_id: int) -> None:
        """Grant to a team; DuplicateError if held, MissingReferenceError if either is gone."""
        with constraints_as_domain_errors("access grant"):
            await self._execute(
                "INSERT INTO registry_credential_team_access (registry_credential_id, team_id) "
                "VALUES (?, ?)",
                (credential_id, team_id),
            )

    async def remove_user(self, credential_id: int, user_id: int) -> bool:
        """Withdraw a user's grant; False if there was none."""
        result = await self._execute(
            "DELETE FROM registry_credential_user_access "
            "WHERE registry_credential_id = ? AND user_id = ?",
            (credential_id, user_id),
        )
        return result.rowcount > 0

    async def remove_team(self, credential_id: int, team_id: int) -> bool:
        """Withdraw a team's grant; False if there was none."""
        result = await self._execute(
            "DELETE FROM registry_credential_team_access "
            "WHERE registry_credential_id = ? AND team_id = ?",
            (credential_id, team_id),
        )
        return result.rowcount > 0

    async def list_environments(
        self, credential_id: int, *, limit: int = MAX_GRANTED_ENVIRONMENTS
    ) -> list[GrantedEnvironment]:
        """List the environments the set is granted to, by name; at most limit."""
        rows = await self._session.fetch_limited(
            "SELECT e.id AS id, e.name AS name FROM registry_environment_access rea "
            "JOIN environment e ON e.id = rea.environment_id "
            "WHERE rea.registry_credential_id = ? ORDER BY e.name COLLATE NOCASE, e.id LIMIT ?",
            (credential_id, limit),
            limit=limit,
        )
        return [GrantedEnvironment(int(row["id"]), str(row["name"])) for row in rows]

    async def count_environments(self, credential_id: int) -> int:
        """Count the environments the set is granted to."""
        row = await self._session.fetch_one(
            "SELECT count(*) AS total FROM registry_environment_access "
            "WHERE registry_credential_id = ?",
            (credential_id,),
        )
        assert row is not None
        return int(row["total"])

    async def list_users(self, credential_id: int, *, limit: int) -> list[GrantedUser]:
        """List users granted directly, by username; at most limit."""
        rows = await self._session.fetch_limited(
            "SELECT a.user_id AS user_id, u.username AS username "
            "FROM registry_credential_user_access a JOIN user u ON u.id = a.user_id "
            "WHERE a.registry_credential_id = ? ORDER BY u.username COLLATE NOCASE, u.id LIMIT ?",
            (credential_id, limit),
            limit=limit,
        )
        return [GrantedUser(int(row["user_id"]), str(row["username"])) for row in rows]

    async def list_teams(self, credential_id: int, *, limit: int) -> list[GrantedTeam]:
        """List teams granted directly, by name; at most limit."""
        rows = await self._session.fetch_limited(
            "SELECT a.team_id AS team_id, t.name AS name "
            "FROM registry_credential_team_access a JOIN team t ON t.id = a.team_id "
            "WHERE a.registry_credential_id = ? ORDER BY t.name COLLATE NOCASE, t.id LIMIT ?",
            (credential_id, limit),
            limit=limit,
        )
        return [GrantedTeam(int(row["team_id"]), str(row["name"])) for row in rows]

    async def count_grants(self, credential_id: int) -> tuple[int, int]:
        """Return how many users and how many teams are granted directly."""
        row = await self._session.fetch_one(
            "SELECT (SELECT count(*) FROM registry_credential_user_access "
            "WHERE registry_credential_id = ?) AS users, "
            "(SELECT count(*) FROM registry_credential_team_access "
            "WHERE registry_credential_id = ?) AS teams",
            (credential_id, credential_id),
        )
        assert row is not None
        return int(row["users"]), int(row["teams"])
