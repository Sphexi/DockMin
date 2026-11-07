"""Option fragments that fill a field's datalist with names of existing objects as it is typed."""

import asyncio
import re
from collections.abc import Coroutine, Sequence
from typing import Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories.stack import StackKind
from app.services.activity import SUBSYSTEMS
from app.services.assembly import Suggestions
from app.services.name_suggestions import (
    EVERY_STACK_KIND,
    EnvironmentKind,
    GlobalKind,
    NameOption,
)
from app.subsystems.base import Subsystem
from app.web.identifiers import PathId
from app.web.navigation import no_store
from app.web.templating import render_partial

router = APIRouter(tags=["suggestions"], dependencies=[Depends(require_user)])

FIELD_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
RULE_TARGETS: Final = "rule-targets"
ENVIRONMENT_KINDS: Final[dict[str, EnvironmentKind]] = {
    "containers": "containers",
    "images": "images",
    "volumes": "volumes",
    "networks": "networks",
    "stacks": "stacks",
    "services": "services",
    "configs": "configs",
    "secrets": "secrets",
}
GLOBAL_KINDS: Final[dict[str, GlobalKind]] = {"users": "users", "grantees": "grantees"}
STACK_KIND_CHOICES: Final[dict[str, frozenset[StackKind]]] = {
    "compose": frozenset({"compose"}),
    "swarm": frozenset({"swarm"}),
    "any": EVERY_STACK_KIND,
}
RULE_TARGET_KINDS: Final[dict[str, EnvironmentKind]] = {
    "container": "containers",
    "stack": "stacks",
    "service": "services",
}


type Answer = tuple[NameOption, ...]


class SuggestionFlights:
    """The one suggestion read running per user and kind in this process."""

    def __init__(self) -> None:
        self._running: dict[tuple[int, str], asyncio.Task[Answer]] = {}

    async def run(self, actor: Principal, kind: str, work: Coroutine[Any, Any, Answer]) -> Answer:
        """Run `work`, cancelling the caller's earlier read of this kind, which answers empty."""
        key = (actor.user_id, kind)
        earlier = self._running.get(key)
        if earlier is not None:
            earlier.cancel()
        task = asyncio.create_task(work)
        self._running[key] = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or current.cancelling() == 0):
                return ()
            raise
        finally:
            if self._running.get(key) is task:
                del self._running[key]


def _flights(request: Request) -> SuggestionFlights:
    flights = getattr(request.app.state, "suggestion_flights", None)
    if not isinstance(flights, SuggestionFlights):
        flights = SuggestionFlights()
        request.app.state.suggestion_flights = flights
    return flights


def _typed(request: Request, field: str | None) -> str | None:
    """Return the text typed into the named field; 404 for a field name that is not one."""
    if field is None or FIELD_NAME.fullmatch(field) is None:
        raise ResourceNotFound("not found")
    values = request.query_params.getlist(field)
    return values[0] if len(values) == 1 else None


def _options(request: Request, options: Sequence[NameOption]) -> Response:
    return no_store(render_partial(request, "name_options", options=options))


def _rule_target(
    request: Request, subsystem: str | None, target_kind: str | None
) -> tuple[EnvironmentKind | None, frozenset[StackKind]]:
    """Return the kind to suggest for a rule target and the stack kinds its automation acts on."""
    chosen = RULE_TARGET_KINDS.get(target_kind or "")
    if chosen is None or subsystem is None or not SUBSYSTEMS.get(subsystem):
        return None, frozenset()
    scheduler = getattr(request.app.state, "scheduler", None)
    handler = None if scheduler is None else scheduler.handlers.get(subsystem)
    if not isinstance(handler, Subsystem) or target_kind not in handler.target_kinds:
        return None, frozenset()
    return chosen, handler.stack_kinds


@router.get(
    "/environments/{environment_id}/suggestions/{kind}",
    response_class=HTMLResponse,
    name="environment_suggestions",
)
async def environment_suggestions(
    request: Request,
    suggestions: Suggestions,
    actor: CurrentUser,
    environment_id: PathId,
    kind: str,
    field: str | None = None,
    stack_kind: str | None = None,
    target_kind: str | None = None,
    subsystem: str | None = None,
) -> Response:
    """Answer with at most 20 options naming objects of `kind` in the environment."""
    if kind == RULE_TARGETS:
        chosen, stack_kinds = _rule_target(request, subsystem, target_kind)
    elif kind in ENVIRONMENT_KINDS:
        chosen = ENVIRONMENT_KINDS[kind]
        stack_kinds = STACK_KIND_CHOICES.get(stack_kind or "any", frozenset())
    else:
        raise ResourceNotFound("not found")
    typed = _typed(request, field)
    if chosen is None or typed is None:
        return _options(request, ())
    found = await _flights(request).run(
        actor,
        kind,
        suggestions.in_environment(actor, environment_id, chosen, typed, stack_kinds=stack_kinds),
    )
    return _options(request, found)


@router.get("/suggestions/{kind}", response_class=HTMLResponse, name="suggestions")
async def global_suggestions(
    request: Request,
    suggestions: Suggestions,
    actor: CurrentUser,
    kind: str,
    field: str | None = None,
) -> Response:
    """Answer with at most 20 options naming users, or users and teams."""
    chosen = GLOBAL_KINDS.get(kind)
    if chosen is None:
        raise ResourceNotFound("not found")
    typed = _typed(request, field)
    if typed is None:
        return _options(request, ())
    found = await _flights(request).run(actor, kind, suggestions.global_names(actor, chosen, typed))
    return _options(request, found)
