"""The environments where a viewer may create from a template."""

from dataclasses import dataclass
from typing import Final

from app.auth.principal import Principal
from app.services.authorisation import Authoriser
from app.services.environments import EnvironmentService

MAX_TARGETS: Final = 50
_PAGE: Final = 100


@dataclass(frozen=True, slots=True)
class TemplateTarget:
    """One environment the viewer may create in."""

    id: int
    name: str
    is_local: bool
    type: str | None


@dataclass(frozen=True, slots=True)
class TargetList:
    """The creatable environments by name, and whether more exist than are listed."""

    items: tuple[TemplateTarget, ...]
    truncated: bool


class TemplateTargetService:
    """Lists the environments a viewer may create containers and stacks in."""

    def __init__(self, environments: EnvironmentService, authoriser: Authoriser) -> None:
        self._environments = environments
        self._authoriser = authoriser

    async def creatable(self, actor: Principal) -> TargetList:
        """Return up to MAX_TARGETS environments the actor may see and create in."""
        found: list[TemplateTarget] = []
        cursor: str | None = None
        while True:
            page = await self._environments.dashboard(actor, limit=_PAGE, cursor=cursor)
            for summary in page.items:
                if not await self._authoriser.may_create(actor, summary.id):
                    continue
                if len(found) == MAX_TARGETS:
                    return TargetList(tuple(found), truncated=True)
                found.append(
                    TemplateTarget(summary.id, summary.name, summary.is_local, summary.type)
                )
            cursor = page.next_cursor
            if cursor is None:
                return TargetList(tuple(found), truncated=False)
