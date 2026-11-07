"""The environments the sidebar lists and the environment the current page belongs to."""

import sqlite3
from dataclasses import dataclass

from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.environment import (
    DashboardQuery,
    DashboardRow,
    EnvironmentDashboardRepository,
)
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.user_session import UserSessionRepository
from app.logging import get_logger

logger = get_logger(__name__)

NAVIGATION_LIMIT = 100
CONNECTION_LABELS = {"unix_socket": "Local socket", "tcp_tls": "TCP+TLS"}


@dataclass(frozen=True, slots=True)
class NavigationCounts:
    containers: int | None
    images: int | None
    volumes: int | None
    networks: int | None
    stacks: int | None
    services: int | None
    nodes: int | None


@dataclass(frozen=True, slots=True)
class NavigationEnvironment:
    id: int
    name: str
    status: str
    connection: str
    engine_version: str | None
    is_swarm: bool
    counts: NavigationCounts | None


@dataclass(frozen=True, slots=True)
class Navigation:
    environments: tuple[NavigationEnvironment, ...]
    current: NavigationEnvironment | None
    remembered: bool = False


async def load_navigation(
    db: Session, actor: Principal, current_id: int | None, remembered_id: int | None = None
) -> Navigation:
    """Return what the caller may see, from stored records only; counts for administrators only.

    Without a current_id the remembered_id stands in, through the same visibility gate.
    """
    dashboard = EnvironmentDashboardRepository(db)
    query = DashboardQuery(visible_to=None if actor.is_administrator else actor.user_id)
    page = await dashboard.page(query, limit=NAVIGATION_LIMIT)
    environments = tuple(_entry(row, actor) for row in page.items)
    remembered = current_id is None and remembered_id is not None
    target_id = remembered_id if remembered else current_id
    current = next((item for item in environments if item.id == target_id), None)
    if current is None and target_id is not None and await _visible(db, actor, target_id):
        row = await dashboard.get(target_id)
        current = None if row is None else _entry(row, actor)
    return Navigation(environments, current, remembered and current is not None)


async def remember_navigation(
    db: Session,
    actor: Principal,
    current_id: int | None,
    remembered_id: int | None,
    navigation: Navigation,
) -> None:
    """Store the environment the sidebar showed on the caller's session; a failure is dropped."""
    if actor.session_id is None:
        return
    if current_id is not None:
        if navigation.current is None or navigation.current.id == remembered_id:
            return
        target: int | None = navigation.current.id
    elif remembered_id is not None and not navigation.remembered:
        target = None
    else:
        return
    try:
        async with db.transaction():
            await UserSessionRepository(db).remember_environment(actor.session_id, target)
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as error:
        logger.debug("navigation_not_remembered", extra={"error": type(error).__name__})


async def load_navigation_environment(
    db: Session, actor: Principal, environment_id: int
) -> NavigationEnvironment | None:
    """Return one environment as the sidebar shows it, or None when the caller may not see it."""
    if not await _visible(db, actor, environment_id):
        return None
    row = await EnvironmentDashboardRepository(db).get(environment_id)
    return None if row is None else _entry(row, actor)


async def _visible(db: Session, actor: Principal, environment_id: int) -> bool:
    if actor.is_administrator:
        return True
    return await EnvironmentAccessRepository(db).gate(actor.user_id, environment_id) is not None


def _entry(row: DashboardRow, actor: Principal) -> NavigationEnvironment:
    return NavigationEnvironment(
        id=row.id,
        name=row.name,
        status=row.status,
        connection=CONNECTION_LABELS.get(row.connection_kind, row.connection_kind),
        engine_version=row.engine_version,
        is_swarm=row.type == "swarm",
        counts=_counts(row) if actor.is_administrator else None,
    )


def _counts(row: DashboardRow) -> NavigationCounts | None:
    if row.snapshot_captured_at is None or row.snapshot_error is not None:
        return None
    return NavigationCounts(
        containers=row.container_count,
        images=row.image_count,
        volumes=row.volume_count,
        networks=row.network_count,
        stacks=row.stack_count,
        services=row.service_count,
        nodes=row.swarm_node_count,
    )
