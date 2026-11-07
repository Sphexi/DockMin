"""Repository for environment_snapshot: the latest object counts of each host."""

import sqlite3
from dataclasses import dataclass, fields, replace

from app.db.engine import Session
from app.db.repositories.base import Repository
from app.db.repositories.errors import constraints_as_domain_errors


@dataclass(frozen=True, slots=True)
class SnapshotCounts:
    """Object counts read from one host; a count a host cannot report stays None."""

    container_count: int | None = None
    container_running_count: int | None = None
    container_stopped_count: int | None = None
    container_paused_count: int | None = None
    container_unhealthy_count: int | None = None
    image_count: int | None = None
    volume_count: int | None = None
    network_count: int | None = None
    stack_count: int | None = None
    service_count: int | None = None
    swarm_node_count: int | None = None


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    """The latest snapshot of one environment, as stored."""

    environment_id: int
    captured_at: str
    counts: SnapshotCounts
    error: str | None


@dataclass(frozen=True, slots=True)
class ObservedTimes:
    """When a host was last checked, contacted and counted, as a refresh that wrote nothing saw."""

    checked_at: str
    contacted_at: str | None
    captured_at: str


class ObservedTimeCache:
    """The newest observation times of each environment, held in memory for the process life."""

    def __init__(self) -> None:
        self._times: dict[int, ObservedTimes] = {}

    def hold(self, environment_id: int, times: ObservedTimes) -> None:
        """Replace the times held for an environment."""
        self._times[environment_id] = times

    def forget(self, environment_id: int) -> None:
        """Drop the times held for an environment."""
        self._times.pop(environment_id, None)

    def checked_at(self, environment_id: int, stored: str | None) -> str | None:
        """Return the later of the stored check time and the one held."""
        held = self._times.get(environment_id)
        return _later(stored, None if held is None else held.checked_at)

    def contacted_at(self, environment_id: int, stored: str | None) -> str | None:
        """Return the later of the stored contact time and the one held."""
        held = self._times.get(environment_id)
        return _later(stored, None if held is None else held.contacted_at)

    def captured_at(self, environment_id: int, stored: str | None) -> str | None:
        """Return the later of the stored capture time and the one held."""
        held = self._times.get(environment_id)
        return _later(stored, None if held is None else held.captured_at)


def _later(stored: str | None, held: str | None) -> str | None:
    if stored is None or held is None or held <= stored:
        return stored
    return held


def observed_times(session: Session) -> ObservedTimeCache:
    """Return the cache shared by every session of the session's database."""
    cache: ObservedTimeCache = session.shared.setdefault(ObservedTimeCache, ObservedTimeCache())
    return cache


_SELECT = (
    "SELECT environment_id, captured_at, container_count, container_running_count, "
    "container_stopped_count, container_paused_count, container_unhealthy_count, image_count, "
    "volume_count, network_count, stack_count, service_count, swarm_node_count, error "
    "FROM environment_snapshot"
)
_UPSERT = (
    "INSERT INTO environment_snapshot (environment_id, captured_at, container_count, "
    "container_running_count, container_stopped_count, container_paused_count, "
    "container_unhealthy_count, image_count, volume_count, network_count, stack_count, "
    "service_count, swarm_node_count, error) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (environment_id) DO UPDATE SET captured_at = excluded.captured_at, "
    "container_count = excluded.container_count, "
    "container_running_count = excluded.container_running_count, "
    "container_stopped_count = excluded.container_stopped_count, "
    "container_paused_count = excluded.container_paused_count, "
    "container_unhealthy_count = excluded.container_unhealthy_count, "
    "image_count = excluded.image_count, volume_count = excluded.volume_count, "
    "network_count = excluded.network_count, stack_count = excluded.stack_count, "
    "service_count = excluded.service_count, swarm_node_count = excluded.swarm_node_count, "
    "error = excluded.error"
)


class EnvironmentSnapshotRepository(Repository[EnvironmentSnapshot]):
    """Reads and writes the one snapshot row each environment has."""

    def _map_row(self, row: sqlite3.Row) -> EnvironmentSnapshot:
        stored = self._stored_from(row)
        captured = observed_times(self._session).captured_at(
            stored.environment_id, stored.captured_at
        )
        return (
            stored if captured == stored.captured_at else replace(stored, captured_at=str(captured))
        )

    def _stored_from(self, row: sqlite3.Row) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            environment_id=int(row["environment_id"]),
            captured_at=str(row["captured_at"]),
            counts=SnapshotCounts(
                container_count=row["container_count"],
                container_running_count=row["container_running_count"],
                container_stopped_count=row["container_stopped_count"],
                container_paused_count=row["container_paused_count"],
                container_unhealthy_count=row["container_unhealthy_count"],
                image_count=row["image_count"],
                volume_count=row["volume_count"],
                network_count=row["network_count"],
                stack_count=row["stack_count"],
                service_count=row["service_count"],
                swarm_node_count=row["swarm_node_count"],
            ),
            error=row["error"],
        )

    async def get(self, environment_id: int) -> EnvironmentSnapshot | None:
        """Fetch an environment's snapshot; its capture time may be newer than the stored one."""
        return await self._one(f"{_SELECT} WHERE environment_id = ?", (environment_id,))

    async def get_stored(self, environment_id: int) -> EnvironmentSnapshot | None:
        """Fetch an environment's snapshot exactly as stored."""
        row = await self._session.fetch_one(
            f"{_SELECT} WHERE environment_id = ?", (environment_id,)
        )
        return None if row is None else self._stored_from(row)

    async def record_counts(
        self, environment_id: int, captured_at: str, counts: SnapshotCounts
    ) -> None:
        """Replace the snapshot with fresh counts; MissingReferenceError if the host is gone."""
        with constraints_as_domain_errors("environment snapshot"):
            await self._execute(
                _UPSERT,
                (
                    environment_id,
                    captured_at,
                    counts.container_count,
                    counts.container_running_count,
                    counts.container_stopped_count,
                    counts.container_paused_count,
                    counts.container_unhealthy_count,
                    counts.image_count,
                    counts.volume_count,
                    counts.network_count,
                    counts.stack_count,
                    counts.service_count,
                    counts.swarm_node_count,
                    None,
                ),
            )

    async def record_error(self, environment_id: int, captured_at: str, error: str) -> None:
        """Replace the snapshot with a failure and null counts, never zeros."""
        with constraints_as_domain_errors("environment snapshot"):
            await self._execute(
                _UPSERT,
                (environment_id, captured_at, *([None] * len(fields(SnapshotCounts))), error),
            )

    async def delete(self, environment_id: int) -> bool:
        """Drop an environment's snapshot; False if it had none."""
        result = await self._execute(
            "DELETE FROM environment_snapshot WHERE environment_id = ?", (environment_id,)
        )
        return result.rowcount > 0
