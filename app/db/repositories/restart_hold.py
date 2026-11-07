"""Repository for restart_hold_sighting: when a held container was last seen unhealthy."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.errors import constraints_as_domain_errors


@dataclass(frozen=True, slots=True)
class HoldSighting:
    """The last time a rule saw one of its held containers unhealthy."""

    container_id: str
    last_unhealthy_at: str


class RestartHoldRepository(ClockedRepository[HoldSighting]):
    """Records and reads sightings; a rule's sightings go with the rule."""

    def _map_row(self, row: sqlite3.Row) -> HoldSighting:
        return HoldSighting(
            container_id=str(row["container_id"]), last_unhealthy_at=str(row["last_unhealthy_at"])
        )

    async def note_unhealthy(self, rule_id: int, environment_id: int, container_id: str) -> None:
        """Stamp the container as seen unhealthy now; an earlier time never replaces a later one.

        Raises MissingReferenceError when the rule or the environment does not exist.
        """
        with constraints_as_domain_errors("restart hold sighting"):
            await self._execute(
                "INSERT INTO restart_hold_sighting (rule_id, environment_id, container_id, "
                "last_unhealthy_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (rule_id, environment_id, container_id) DO UPDATE SET "
                "last_unhealthy_at = excluded.last_unhealthy_at "
                "WHERE excluded.last_unhealthy_at >= restart_hold_sighting.last_unhealthy_at",
                (rule_id, environment_id, container_id, self._now()),
            )

    async def sightings(
        self, rule_id: int, environment_id: int, container_ids: Sequence[str]
    ) -> dict[str, str]:
        """Return the stored sighting time of each listed container the rule has seen."""
        if not container_ids:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT container_id, last_unhealthy_at FROM restart_hold_sighting "
            "WHERE rule_id = ? AND environment_id = ? "
            "AND container_id IN (SELECT value FROM json_each(?))",
            (rule_id, environment_id, json.dumps(list(container_ids))),
            limit=len(container_ids),
        )
        return {found.container_id: found.last_unhealthy_at for found in map(self._map_row, rows)}
