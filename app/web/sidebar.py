"""Loads the sidebar's environments for a full page; fragments, API calls and sockets load none."""

from fastapi import Request
from starlette.requests import HTTPConnection

from app.auth.bearer import is_api_path, is_bearer_request
from app.auth.principal import Principal
from app.db.engine import Database, DatabaseClosed
from app.services.environment_snapshots import SnapshotPokes
from app.services.navigation import load_navigation, remember_navigation
from app.web.templating import is_htmx_request

MAX_ENVIRONMENT_ID = 2**63 - 1


async def resolve_navigation(connection: HTTPConnection) -> None:
    """Attach the sidebar's environments, or None, to the connection state for base.html."""
    connection.state.navigation = None
    connection.state.nav_counts_pending = False
    if not isinstance(connection, Request):
        return
    principal: Principal | None = getattr(connection.state, "principal", None)
    if (
        principal is None
        or is_htmx_request(connection)
        or is_api_path(connection)
        or is_bearer_request(connection)
    ):
        return
    database: Database | None = getattr(connection.app.state, "database", None)
    if database is None:
        return
    current_id = environment_id_of(connection)
    remembered_id = principal.remembered_environment_id
    try:
        async with database.session() as db:
            navigation = await load_navigation(db, principal, current_id, remembered_id)
            connection.state.navigation = navigation
            await remember_navigation(db, principal, current_id, remembered_id, navigation)
    except DatabaseClosed:
        connection.state.navigation = None
        return
    connection.state.nav_counts_pending = (
        principal.is_administrator
        and current_id is not None
        and _counts_refresh_pending(connection, current_id)
    )


def snapshot_pokes(connection: HTTPConnection) -> SnapshotPokes | None:
    """Return the application's refresh queue, or None when it has no real one."""
    refresher = getattr(getattr(connection.app.state, "engine", None), "refresher", None)
    return refresher if isinstance(refresher, SnapshotPokes) else None


def _counts_refresh_pending(connection: HTTPConnection, environment_id: int) -> bool:
    pokes = snapshot_pokes(connection)
    return pokes is not None and pokes.is_pending(environment_id)


def environment_id_of(request: Request) -> int | None:
    raw = request.path_params.get("environment_id")
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 19:
        return None
    value = int(raw)
    return value if 1 <= value <= MAX_ENVIRONMENT_ID else None
