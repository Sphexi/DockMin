"""Read-only join of a team's memberships with the usernames behind them."""

import sqlite3
from dataclasses import dataclass

from app.db.repositories.base import row_as
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.pagination import Page

_SELECT = (
    "SELECT m.id AS id, m.team_id AS team_id, m.user_id AS user_id, u.username AS username, "
    "m.is_leader AS is_leader, m.created_at AS created_at "
    "FROM team_membership m JOIN user u ON u.id = m.user_id"
)


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One member of a team, with the account's username."""

    id: int
    team_id: int
    user_id: int
    username: str
    is_leader: bool
    created_at: str


class TeamRosterRepository(ClockedRepository[RosterEntry]):
    """Lists who is on a team."""

    def _map_row(self, row: sqlite3.Row) -> RosterEntry:
        return row_as(RosterEntry, {**dict(row), "is_leader": bool(row["is_leader"])})

    async def list_for_team(
        self, team_id: int, *, limit: int, cursor: str | None = None
    ) -> Page[RosterEntry]:
        """Return one page of a team's members in the order they joined."""
        return await self._page(
            _SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="m.id",
            where="m.team_id = ?",
            params=(team_id,),
            id_column="m.id",
        )
