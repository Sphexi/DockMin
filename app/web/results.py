"""Short-lived store for the per-target outcome of a post, so a refresh repeats nothing."""

import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from app.auth.dependencies import CurrentUser
from app.auth.errors import NotFound, PermissionDenied
from app.web.navigation import no_store, redirect
from app.web.templating import render_page

RESULT_TTL_SECONDS: Final = 600.0
MAX_RESULTS: Final = 200
MAX_RESULTS_PER_USER: Final = 20
TOKEN_BYTES: Final = 24
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{32}")

router = APIRouter()


@dataclass(frozen=True, slots=True)
class _Entry:
    user_id: int
    template: str
    context: Mapping[str, Any]
    expires_at: float


class ResultStore:
    """Holds page inputs for ten minutes: MAX_RESULTS_PER_USER per user, MAX_RESULTS overall."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def put(self, user_id: int, template: str, context: Mapping[str, Any]) -> str:
        """Store a result for the user and return its token."""
        self._drop_expired()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._entries[token] = _Entry(
            user_id, template, dict(context), self._clock() + RESULT_TTL_SECONDS
        )
        own = [key for key, entry in self._entries.items() if entry.user_id == user_id]
        for key in own[: max(0, len(own) - MAX_RESULTS_PER_USER)]:
            del self._entries[key]
        while len(self._entries) > MAX_RESULTS:
            self._entries.popitem(last=False)
        return token

    def forget_user(self, user_id: int) -> None:
        """Drop every stored result of the user."""
        for key in [key for key, entry in self._entries.items() if entry.user_id == user_id]:
            del self._entries[key]

    def get(self, user_id: int, token: str) -> tuple[str, Mapping[str, Any]] | None:
        """Return the template and context, or None for an unknown, expired or foreign token."""
        self._drop_expired()
        entry = self._entries.get(token)
        if entry is None or entry.user_id != user_id:
            return None
        return entry.template, entry.context

    def _drop_expired(self) -> None:
        now = self._clock()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            del self._entries[token]


def forget_results(request: Request, user_id: int) -> None:
    """Drop the stored results of a user whose sign-in, password or role just changed."""
    store: ResultStore = request.app.state.results
    store.forget_user(user_id)


def show_result(request: Request, template: str, context: Mapping[str, Any]) -> Response:
    """Store a per-target outcome and redirect to the page that shows it.

    The context must hold view objects ready for the template, never a secret.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise PermissionDenied
    store: ResultStore = request.app.state.results
    token = store.put(principal.user_id, template, context)
    return no_store(redirect(request, str(request.app.url_path_for("action_result", token=token))))


@router.get("/results/{token}", response_class=HTMLResponse, name="action_result")
async def action_result(request: Request, actor: CurrentUser, token: str) -> Response:
    """Show a stored outcome; repeating the request shows it again and acts on nothing."""
    store: ResultStore = request.app.state.results
    stored = store.get(actor.user_id, token) if _TOKEN.fullmatch(token) else None
    if stored is None:
        raise NotFound
    template, context = stored
    return no_store(render_page(request, template, dict(context)))
