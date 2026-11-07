"""Repository for the team_membership table."""

import sqlite3
from dataclasses import dataclass

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

_SELECT = "SELECT id, team_id, user_id, is_leader, created_at FROM team_membership"


@dataclass(frozen=True, slots=True)
class TeamMembership:
    """A user's membership of one team."""

    id: int
    team_id: int
    user_id: int
    is_leader: bool
    created_at: str


class TeamMembershipRepository(ClockedRepository[TeamMembership]):
    """Reads and writes memberships, addressed by (team, user)."""

    def _map_row(self, row: sqlite3.Row) -> TeamMembership:
        return row_as(TeamMembership, {**dict(row), "is_leader": bool(row["is_leader"])})

    async def add(self, team_id: int, user_id: int, *, is_leader: bool = False) -> TeamMembership:
        """Add a user to a team.

        Raises DuplicateError if already a member, MissingReferenceError if either id is unknown.
        """
        with constraints_as_domain_errors("team membership"):
            result = await self._execute(
                "INSERT INTO team_membership (team_id, user_id, is_leader, created_at) "
                "VALUES (?, ?, ?, ?)",
                (team_id, user_id, int(is_leader), self._now()),
            )
        return await self._created(result, self.get)

    async def get(self, membership_id: int) -> TeamMembership | None:
        """Fetch a membership by id."""
        return await self._one(f"{_SELECT} WHERE id = ?", (membership_id,))

    async def get_for(self, team_id: int, user_id: int) -> TeamMembership | None:
        """Fetch the membership of one user in one team."""
        return await self._one(f"{_SELECT} WHERE team_id = ? AND user_id = ?", (team_id, user_id))

    async def list_for_team(
        self, team_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[TeamMembership]:
        """Return one page of a team's memberships in the order they were added."""
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="id",
            where="team_id = ?",
            params=(team_id,),
        )

    async def list_for_user(
        self, user_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[TeamMembership]:
        """Return one page of a user's memberships in the order they were added."""
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="id",
            where="user_id = ?",
            params=(user_id,),
        )

    async def set_leader(self, team_id: int, user_id: int, *, is_leader: bool) -> bool:
        """Grant or revoke leadership; return False if the user is not in the team."""
        result = await self._execute(
            "UPDATE team_membership SET is_leader = ? WHERE team_id = ? AND user_id = ?",
            (int(is_leader), team_id, user_id),
        )
        return result.rowcount > 0

    async def remove(self, team_id: int, user_id: int) -> bool:
        """Remove a user from a team; return False if the user was not a member."""
        result = await self._execute(
            "DELETE FROM team_membership WHERE team_id = ? AND user_id = ?", (team_id, user_id)
        )
        return result.rowcount > 0
