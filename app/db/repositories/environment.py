"""Repositories for the environment table: rows, status, and the dashboard page."""

import json
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.db.repositories.base import Repository
from app.db.repositories.clocked import ClockedRepository
from app.db.repositories.environment_access import environment_visible_clause
from app.db.repositories.environment_snapshot import observed_times
from app.db.repositories.errors import constraints_as_domain_errors
from app.db.repositories.pagination import Page

ConnectionKind = Literal["unix_socket", "tcp_tls"]
EnvironmentStatus = Literal["unknown", "reachable", "unreachable", "version_unsupported"]
FailedStage = Literal["dns", "tcp", "socket", "tls_handshake", "certificate", "engine"]
EnvironmentType = Literal["standalone", "swarm"]
SwarmState = Literal["inactive", "pending", "active", "error", "locked"]

MAX_TAG_FILTERS = 10
MAX_NAME_FILTER = 64
FOLD_SCAN_PAGE = 500

_COLUMNS = (
    "id, name, type, connection_kind, socket_path, url, environment_group_id, status, "
    "failed_stage, status_message, last_checked_at, last_contacted_at, engine_version, "
    "api_version, swarm_state, swarm_control_available, compose_cli_version, engine_host_name, "
    "created_at, updated_at"
)
_SELECT = f"SELECT {_COLUMNS} FROM environment"


@dataclass(frozen=True, slots=True)
class Environment:
    """A managed Docker host as stored."""

    id: int
    name: str
    type: EnvironmentType | None
    connection_kind: ConnectionKind
    socket_path: str | None
    url: str | None
    environment_group_id: int | None
    status: EnvironmentStatus
    failed_stage: FailedStage | None
    status_message: str | None
    last_checked_at: str | None
    last_contacted_at: str | None
    engine_version: str | None
    api_version: str | None
    swarm_state: SwarmState | None
    swarm_control_available: bool | None
    compose_cli_version: str | None
    engine_host_name: str | None
    created_at: str
    updated_at: str

    @property
    def endpoint(self) -> str:
        """Return the stored address of the host."""
        address = self.socket_path if self.connection_kind == "unix_socket" else self.url
        assert address is not None
        return address


@dataclass(frozen=True, slots=True)
class StatusRecord:
    """What an observation of a host writes to its row.

    Facts left as None keep the value last observed, so an unreachable host still shows the
    versions it last reported. contacted_at is set only by an observation that reached it.
    """

    status: EnvironmentStatus
    failed_stage: FailedStage | None
    message: str | None
    checked_at: str
    contacted_at: str | None = None
    engine_version: str | None = None
    api_version: str | None = None
    type: EnvironmentType | None = None
    swarm_state: SwarmState | None = None
    swarm_control_available: bool | None = None
    compose_cli_version: str | None = None
    engine_host_name: str | None = None


@dataclass(frozen=True, slots=True)
class GroupCandidate:
    """An environment that could join a group, and the group it is in now."""

    id: int
    name: str
    group_id: int | None
    group_name: str | None


@dataclass(frozen=True, slots=True)
class EnvironmentDependents:
    """How many records of each kind are deleted with an environment."""

    tls_material: int
    tag_links: int
    access_grants: int
    registry_links: int
    image_bindings: int
    stacks: int
    automation_rules: int
    scheduled_jobs: int
    job_runs: int
    snapshots: int
    resource_controls: Mapping[str, int]


def _bool_or_none(value: Any) -> bool | None:
    return None if value is None else bool(value)


class EnvironmentRepository(ClockedRepository[Environment]):
    """Reads and writes environments; names are unique ignoring ASCII case."""

    def _map_row(self, row: sqlite3.Row) -> Environment:
        stored = self._stored_from(row)
        observed = observed_times(self._session)
        return replace(
            stored,
            last_checked_at=observed.checked_at(stored.id, stored.last_checked_at),
            last_contacted_at=observed.contacted_at(stored.id, stored.last_contacted_at),
        )

    def _stored_from(self, row: sqlite3.Row) -> Environment:
        values = dict(row)
        values["swarm_control_available"] = _bool_or_none(values["swarm_control_available"])
        return Environment(**values)

    async def create(
        self,
        *,
        name: str,
        connection_kind: ConnectionKind,
        socket_path: str | None,
        url: str | None,
        record: StatusRecord,
    ) -> Environment:
        """Insert an environment; raise DuplicateError if its name or endpoint is taken."""
        now = self._now()
        with constraints_as_domain_errors("environment"):
            result = await self._execute(
                "INSERT INTO environment (name, type, connection_kind, socket_path, url, status, "
                "failed_stage, status_message, last_checked_at, last_contacted_at, engine_version, "
                "api_version, swarm_state, swarm_control_available, engine_host_name, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    record.type,
                    connection_kind,
                    socket_path,
                    url,
                    record.status,
                    record.failed_stage,
                    record.message,
                    record.checked_at,
                    record.contacted_at,
                    record.engine_version,
                    record.api_version,
                    record.swarm_state,
                    _int_or_none(record.swarm_control_available),
                    record.engine_host_name,
                    now,
                    now,
                ),
            )
        return await self._created(result, self.get)

    async def get(self, environment_id: int) -> Environment | None:
        """Fetch an environment by id; check times may be newer than the stored ones."""
        return await self._one(f"{_SELECT} WHERE id = ?", (environment_id,))

    async def get_stored(self, environment_id: int) -> Environment | None:
        """Fetch an environment by id exactly as stored."""
        row = await self._session.fetch_one(f"{_SELECT} WHERE id = ?", (environment_id,))
        return None if row is None else self._stored_from(row)

    async def get_by_name(self, name: str) -> Environment | None:
        """Fetch an environment by name, ignoring ASCII case."""
        return await self._one(f"{_SELECT} WHERE name = ? COLLATE NOCASE", (name,))

    async def names(self, environment_ids: Collection[int]) -> dict[int, str]:
        """Map each id that names an existing environment to its current name."""
        distinct = sorted(set(environment_ids))
        if not distinct:
            return {}
        rows = await self._session.fetch_limited(
            "SELECT id, name FROM environment WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(distinct),),
            limit=len(distinct),
        )
        return {int(row["id"]): str(row["name"]) for row in rows}

    async def find_by_endpoint(
        self, connection_kind: ConnectionKind, endpoint: str
    ) -> Environment | None:
        """Fetch the environment at an endpoint; the kind selects the partial unique index."""
        if connection_kind == "unix_socket":
            return await self._one(
                f"{_SELECT} WHERE connection_kind = 'unix_socket' AND socket_path = ?",
                (endpoint,),
            )
        return await self._one(
            f"{_SELECT} WHERE connection_kind = 'tcp_tls' AND url = ?", (endpoint,)
        )

    async def rename(self, environment_id: int, name: str) -> bool:
        """Rename an environment; False if it does not exist, DuplicateError on a clash."""
        with constraints_as_domain_errors("environment"):
            result = await self._execute(
                "UPDATE environment SET name = ?, updated_at = ? WHERE id = ?",
                (name, self._now(), environment_id),
            )
        return result.rowcount > 0

    async def replace_connection(
        self,
        environment_id: int,
        *,
        connection_kind: ConnectionKind,
        socket_path: str | None,
        url: str | None,
        record: StatusRecord,
    ) -> bool:
        """Point an environment at a new endpoint, replacing what was observed about the old one."""
        with constraints_as_domain_errors("environment"):
            result = await self._execute(
                "UPDATE environment SET type = ?, connection_kind = ?, socket_path = ?, url = ?, "
                "status = ?, failed_stage = ?, status_message = ?, last_checked_at = ?, "
                "last_contacted_at = ?, engine_version = ?, api_version = ?, swarm_state = ?, "
                "swarm_control_available = ?, engine_host_name = ?, updated_at = ? WHERE id = ?",
                (
                    record.type,
                    connection_kind,
                    socket_path,
                    url,
                    record.status,
                    record.failed_stage,
                    record.message,
                    record.checked_at,
                    record.contacted_at,
                    record.engine_version,
                    record.api_version,
                    record.swarm_state,
                    _int_or_none(record.swarm_control_available),
                    record.engine_host_name,
                    self._now(),
                    environment_id,
                ),
            )
        return result.rowcount > 0

    async def record_status(self, environment_id: int, record: StatusRecord) -> bool:
        """Store an observation; False if the environment is gone.

        failed_stage is written in the same statement as status so the table's check never sees
        a stage left behind by a host that has recovered. Does not touch updated_at, which marks
        configuration changes.
        """
        result = await self._execute(
            "UPDATE environment SET status = ?, failed_stage = ?, status_message = ?, "
            "last_checked_at = ?, "
            "last_contacted_at = COALESCE(?, last_contacted_at), "
            "engine_version = COALESCE(?, engine_version), "
            "api_version = COALESCE(?, api_version), "
            "type = COALESCE(?, type), "
            "swarm_state = COALESCE(?, swarm_state), "
            "swarm_control_available = COALESCE(?, swarm_control_available), "
            "compose_cli_version = COALESCE(?, compose_cli_version), "
            "engine_host_name = COALESCE(?, engine_host_name) "
            "WHERE id = ?",
            (
                record.status,
                record.failed_stage,
                record.message,
                record.checked_at,
                record.contacted_at,
                record.engine_version,
                record.api_version,
                record.type,
                record.swarm_state,
                _int_or_none(record.swarm_control_available),
                record.compose_cli_version,
                record.engine_host_name,
                environment_id,
            ),
        )
        return result.rowcount > 0

    async def record_compose_version(self, environment_id: int, version: str) -> bool:
        """Store the Compose version that last served this environment; False if it is gone.

        Like record_status, leaves updated_at alone.
        """
        result = await self._execute(
            "UPDATE environment SET compose_cli_version = ? WHERE id = ?",
            (version, environment_id),
        )
        return result.rowcount > 0

    async def set_group(self, environment_id: int, group_id: int | None) -> bool:
        """Place an environment in a group, or ungroup it; False if it does not exist."""
        with constraints_as_domain_errors("environment group"):
            result = await self._execute(
                "UPDATE environment SET environment_group_id = ?, updated_at = ? WHERE id = ?",
                (group_id, self._now(), environment_id),
            )
        return result.rowcount > 0

    async def delete(self, environment_id: int) -> bool:
        """Delete an environment and, by cascade, everything scoped to it."""
        result = await self._execute("DELETE FROM environment WHERE id = ?", (environment_id,))
        observed_times(self._session).forget(environment_id)
        return result.rowcount > 0

    async def list_in_group(self, group_id: int, *, limit: int) -> list[tuple[int, str]]:
        """List (id, name) of the environments in a group, by name; at most limit."""
        rows = await self._session.fetch_limited(
            "SELECT id, name FROM environment WHERE environment_group_id = ? "
            "ORDER BY name, id LIMIT ?",
            (group_id, limit),
            limit=limit,
        )
        return [(int(row["id"]), str(row["name"])) for row in rows]

    async def list_candidates(
        self, group_id: int, *, name_contains: str | None, limit: int
    ) -> list[GroupCandidate]:
        """List the environments outside a group, by name, with the group each is in now."""
        conditions = ["(e.environment_group_id IS NULL OR e.environment_group_id != ?)"]
        params: list[Any] = [group_id]
        if name_contains:
            conditions.append("e.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(name_contains)}%")
        rows = await self._session.fetch_limited(
            "SELECT e.id AS id, e.name AS name, g.id AS group_id, g.name AS group_name "
            "FROM environment e LEFT JOIN environment_group g ON g.id = e.environment_group_id "
            f"WHERE {' AND '.join(conditions)} ORDER BY e.name, e.id LIMIT ?",
            (*params, limit),
            limit=limit,
        )
        return [
            GroupCandidate(
                id=int(row["id"]),
                name=str(row["name"]),
                group_id=None if row["group_id"] is None else int(row["group_id"]),
                group_name=None if row["group_name"] is None else str(row["group_name"]),
            )
            for row in rows
        ]

    async def find_by_folded_name(
        self, name: str, *, excluding: int | None = None
    ) -> Environment | None:
        """Find an environment whose name equals this one under full Unicode case folding.

        The column folds ASCII only, so a name differing in the case of a non-ASCII letter passes
        the unique index; this closes that gap. Callers run it inside the write transaction.
        """
        folded = name.casefold()
        after_id = 0
        while True:
            rows = await self._session.fetch_limited(
                "SELECT id, name FROM environment WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, FOLD_SCAN_PAGE),
                limit=FOLD_SCAN_PAGE,
            )
            if not rows:
                return None
            for row in rows:
                if int(row["id"]) != excluding and str(row["name"]).casefold() == folded:
                    return await self.get(int(row["id"]))
            after_id = int(rows[-1]["id"])

    async def dependents(self, environment_id: int) -> EnvironmentDependents:
        """Count what deleting the environment would remove with it."""
        row = await self._session.fetch_one(
            "SELECT "
            "(SELECT count(*) FROM environment_tls_material WHERE environment_id = ?) AS tls, "
            "(SELECT count(*) FROM environment_tag WHERE environment_id = ?) AS tags, "
            "(SELECT count(*) FROM environment_user_access WHERE environment_id = ?) AS users, "
            "(SELECT count(*) FROM environment_team_access WHERE environment_id = ?) AS teams, "
            "(SELECT count(*) FROM registry_environment_access WHERE environment_id = ?) AS reg, "
            "(SELECT count(*) FROM image_credential_binding WHERE environment_id = ?) AS bound, "
            "(SELECT count(*) FROM stack WHERE environment_id = ?) AS stacks, "
            "(SELECT count(*) FROM automation_rule WHERE environment_id = ?) AS rules, "
            "(SELECT count(*) FROM scheduled_job j JOIN automation_rule r ON r.id = j.rule_id "
            "WHERE r.environment_id = ?) AS jobs, "
            "(SELECT count(*) FROM job_run jr JOIN scheduled_job sj ON sj.id = jr.scheduled_job_id "
            "WHERE sj.environment_id = ?) AS runs, "
            "(SELECT count(*) FROM environment_snapshot WHERE environment_id = ?) AS snaps",
            (environment_id,) * 11,
        )
        assert row is not None
        controls = await self._session.fetch_limited(
            "SELECT resource_type, count(*) AS total FROM resource_control "
            "WHERE environment_id = ? GROUP BY resource_type ORDER BY resource_type LIMIT ?",
            (environment_id, 16),
            limit=16,
        )
        return EnvironmentDependents(
            tls_material=int(row["tls"]),
            tag_links=int(row["tags"]),
            access_grants=int(row["users"]) + int(row["teams"]),
            registry_links=int(row["reg"]),
            image_bindings=int(row["bound"]),
            stacks=int(row["stacks"]),
            automation_rules=int(row["rules"]),
            scheduled_jobs=int(row["jobs"]),
            job_runs=int(row["runs"]),
            snapshots=int(row["snaps"]),
            resource_controls={str(c["resource_type"]): int(c["total"]) for c in controls},
        )

    async def list_ids_page(self, *, limit: int, after_id: int = 0) -> list[int]:
        """Return up to limit environment ids greater than after_id, ascending."""
        rows = await self._session.fetch_limited(
            "SELECT id FROM environment WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit),
            limit=limit,
        )
        return [int(row["id"]) for row in rows]


def _int_or_none(value: bool | None) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True, slots=True)
class DashboardQuery:
    """The filters of the environment list; every field left unset is unconstrained."""

    visible_to: int | None = None
    group_id: int | None = None
    ungrouped: bool = False
    status: EnvironmentStatus | None = None
    name_contains: str | None = None
    tag_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.group_id is not None and self.ungrouped:
            raise ValueError("a group and ungrouped cannot both be requested")
        if len(self.tag_ids) > MAX_TAG_FILTERS:
            raise ValueError(f"at most {MAX_TAG_FILTERS} tags can be filtered on")
        if self.name_contains is not None and len(self.name_contains) > MAX_NAME_FILTER:
            raise ValueError(f"the name filter is at most {MAX_NAME_FILTER} characters")


@dataclass(frozen=True, slots=True)
class DashboardRow:
    """One environment with its group and its cached snapshot joined in."""

    id: int
    name: str
    type: EnvironmentType | None
    connection_kind: ConnectionKind
    socket_path: str | None
    url: str | None
    group_id: int | None
    group_name: str | None
    status: EnvironmentStatus
    failed_stage: FailedStage | None
    status_message: str | None
    last_checked_at: str | None
    last_contacted_at: str | None
    engine_version: str | None
    api_version: str | None
    swarm_state: SwarmState | None
    swarm_control_available: bool | None
    compose_cli_version: str | None
    created_at: str
    snapshot_captured_at: str | None
    container_count: int | None
    container_running_count: int | None
    container_stopped_count: int | None
    container_paused_count: int | None
    container_unhealthy_count: int | None
    image_count: int | None
    volume_count: int | None
    network_count: int | None
    stack_count: int | None
    service_count: int | None
    swarm_node_count: int | None
    snapshot_error: str | None


_DASHBOARD_SELECT = (
    "SELECT e.id AS id, e.name AS name, e.type AS type, e.connection_kind AS connection_kind, "
    "e.socket_path AS socket_path, e.url AS url, g.id AS group_id, g.name AS group_name, "
    "e.status AS status, e.failed_stage AS failed_stage, e.status_message AS status_message, "
    "e.last_checked_at AS last_checked_at, e.last_contacted_at AS last_contacted_at, "
    "e.engine_version AS engine_version, e.api_version AS api_version, "
    "e.swarm_state AS swarm_state, e.swarm_control_available AS swarm_control_available, "
    "e.compose_cli_version AS compose_cli_version, e.created_at AS created_at, "
    "s.captured_at AS snapshot_captured_at, s.container_count AS container_count, "
    "s.container_running_count AS container_running_count, "
    "s.container_stopped_count AS container_stopped_count, "
    "s.container_paused_count AS container_paused_count, "
    "s.container_unhealthy_count AS container_unhealthy_count, s.image_count AS image_count, "
    "s.volume_count AS volume_count, s.network_count AS network_count, "
    "s.stack_count AS stack_count, s.service_count AS service_count, "
    "s.swarm_node_count AS swarm_node_count, s.error AS snapshot_error "
    "FROM environment e "
    "LEFT JOIN environment_group g ON g.id = e.environment_group_id "
    "LEFT JOIN environment_snapshot s ON s.environment_id = e.id"
)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class EnvironmentDashboardRepository(Repository[DashboardRow]):
    """The filtered, keyset-paged environment list the dashboard shows."""

    def _map_row(self, row: sqlite3.Row) -> DashboardRow:
        values = dict(row)
        values["swarm_control_available"] = _bool_or_none(values["swarm_control_available"])
        observed = observed_times(self._session)
        environment_id = values["id"]
        values["last_checked_at"] = observed.checked_at(environment_id, values["last_checked_at"])
        values["last_contacted_at"] = observed.contacted_at(
            environment_id, values["last_contacted_at"]
        )
        values["snapshot_captured_at"] = observed.captured_at(
            environment_id, values["snapshot_captured_at"]
        )
        return DashboardRow(**values)

    async def get(self, environment_id: int) -> DashboardRow | None:
        """Fetch one environment with its group and snapshot joined in."""
        return await self._one(f"{_DASHBOARD_SELECT} WHERE e.id = ?", (environment_id,))

    @staticmethod
    def _filter(query: DashboardQuery) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if query.visible_to is not None:
            fragment = environment_visible_clause(
                query.visible_to, "e.id", "e.environment_group_id"
            )
            conditions.append(fragment.clause)
            params.extend(fragment.params)
        if query.group_id is not None:
            conditions.append("e.environment_group_id = ?")
            params.append(query.group_id)
        if query.ungrouped:
            conditions.append("e.environment_group_id IS NULL")
        if query.status is not None:
            conditions.append("e.status = ?")
            params.append(query.status)
        if query.name_contains:
            conditions.append("e.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(query.name_contains)}%")
        for tag_id in query.tag_ids:
            conditions.append(
                "EXISTS (SELECT 1 FROM environment_tag et "
                "WHERE et.environment_id = e.id AND et.tag_id = ?)"
            )
            params.append(tag_id)
        return " AND ".join(conditions) or "1 = 1", params

    async def page(
        self, query: DashboardQuery, *, limit: int, cursor: str | None = None
    ) -> Page[DashboardRow]:
        """Return one page ordered by name, restricted to what the query's user may see."""
        where, params = self._filter(query)
        return await self._page(
            _DASHBOARD_SELECT,
            limit=limit,
            cursor=cursor,
            sort_column="e.name",
            id_column="e.id",
            where=where,
            params=params,
        )

    async def count(self, query: DashboardQuery) -> int:
        """Count the environments page lists for the same query, across all pages."""
        where, params = self._filter(query)
        row = await self._session.fetch_one(
            f"SELECT COUNT(*) AS total FROM environment e WHERE {where}", tuple(params)
        )
        return 0 if row is None else int(row["total"])
