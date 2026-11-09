"""The stacks named on a page whose own page the viewer may open."""

from collections.abc import Iterable

from fastapi import Request

from app.auth.principal import Principal
from app.db.repositories.stack import StackKind
from app.services.assembly import app_stack_services


async def stack_links(
    request: Request,
    actor: Principal,
    environment_id: int,
    refs: Iterable[tuple[StackKind | None, str | None]],
) -> dict[str, str]:
    """Map `kind:name` of each stack the viewer may open to the name its page is opened by."""
    wanted = [(kind, name) for kind, name in refs if kind is not None and name]
    if not wanted:
        return {}
    async with app_stack_services(request.app) as stacks:
        return await stacks.queries.stack_links(actor, environment_id, wanted)
