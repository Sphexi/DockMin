"""Reads environments and containers for the self-identity resolver."""

from typing import Final

from starlette.applications import Starlette

from app.db.repositories.environment import DashboardQuery, EnvironmentDashboardRepository
from app.docker_engine.errors import EngineError, NotFound
from app.self_identity import ContainerFacts, EnvironmentRef
from app.services.access import COMPOSE_PROJECT_LABEL
from app.services.assembly import build_host_clients
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.services.containers import display_name, short_id

ENVIRONMENT_PAGE_SIZE: Final = 200
SWARM_NODE_LABEL: Final = "com.docker.swarm.node.id"
NOT_A_MANAGER_STATUS: Final = 503


class EngineSelfProbe:
    """Answers the resolver's questions; no database session is open while an Engine call runs."""

    def __init__(self, app: Starlette) -> None:
        self._app = app

    async def environments(self) -> list[EnvironmentRef]:
        """Return every environment's id, connection kind and whether it is a Swarm."""
        state = self._app.state
        refs: list[EnvironmentRef] = []
        cursor: str | None = None
        async with state.database.session() as db:
            repository = EnvironmentDashboardRepository(db)
            while True:
                page = await repository.page(
                    DashboardQuery(), limit=ENVIRONMENT_PAGE_SIZE, cursor=cursor
                )
                refs.extend(
                    EnvironmentRef(row.id, row.connection_kind, row.type == "swarm")
                    for row in page.items
                )
                cursor = page.next_cursor
                if cursor is None:
                    return refs

    async def inspect(self, ref: EnvironmentRef, container_id: str) -> ContainerFacts | None:
        """Return the container as the environment reports it, or None when it holds no such id.

        A Swarm environment that does not hold the container itself is asked for its running
        tasks, and a task whose container id is exactly this one stands in for it.
        """
        state = self._app.state
        async with state.database.session() as db:
            client = await build_host_clients(db, state.auth, state.engine).client(
                ref.environment_id
            )
        try:
            found = await client.containers.inspect(container_id)
        except NotFound:
            found = None
        if found is not None and found.id != container_id:
            found = None
        if found is not None:
            labels = found.config.labels
            return ContainerFacts(
                container_id=found.id,
                name=display_name([found.name], found.id),
                hostname=found.config.hostname,
                volumes=frozenset(m.name for m in found.mounts if m.type == "volume" and m.name),
                mount_sources=frozenset(
                    m.source for m in found.mounts if m.source.startswith("/") and m.destination
                ),
                compose_project=labels.get(COMPOSE_PROJECT_LABEL) or None,
                swarm_service_id=labels.get(SWARM_SERVICE_LABEL) or None,
                swarm_node_id=labels.get(SWARM_NODE_LABEL) or None,
            )
        if not ref.is_swarm:
            return None
        try:
            tasks = await client.swarm_tasks.list(filters={"desired-state": ["running"]})
        except EngineError as error:
            if error.status_code == NOT_A_MANAGER_STATUS:
                return None
            raise
        for task in tasks:
            status = task.status.container_status
            if status is not None and status.container_id == container_id:
                return ContainerFacts(
                    container_id=container_id,
                    name=short_id(container_id),
                    hostname=None,
                    swarm_service_id=task.service_id or None,
                    swarm_node_id=task.node_id or None,
                )
        return None
