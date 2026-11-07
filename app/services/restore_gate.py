"""The offline gate: refuses requests while a restore or re-key holds the instance."""

import asyncio
from datetime import datetime
from typing import Final, Literal

from fastapi import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.bearer import is_api_path
from app.logging import get_logger
from app.paths import HEALTH_PATH
from app.web.csrf import SAFE_METHODS
from app.web.navigation import no_store
from app.web.templating import is_htmx_request, render_page, render_partial

type GatePhase = Literal["open", "engaged", "swapping", "failed"]
type GatePurpose = Literal["restore", "rekey"]

logger = get_logger(__name__)

OFFLINE_MESSAGE: Final = (
    "This instance is offline for a restore. Changes are refused until the restore finishes "
    "or is cancelled."
)
REKEY_MESSAGE: Final = (
    "This instance is re-encrypting stored secrets. Changes are refused until it finishes."
)
SWAPPING_MESSAGE: Final = (
    "This instance is replacing its data from a backup. Requests are refused until the restore "
    "finishes."
)
FAILED_MESSAGE: Final = (
    "A restore failed and the data could not be put back. Restore the files by hand from the "
    "paths below, then restart the container."
)
AUTOMATION_PAUSED_MESSAGE: Final = (
    "Automation is paused after a restore. Scheduled jobs do not run until an administrator "
    "resumes it."
)
RETRY_AFTER: Final = {"engaged": "30", "swapping": "5", "failed": "30"}
CLOSE_TRY_AGAIN_LATER: Final = 1013
LOGIN_PATH: Final = "/login"
LOGIN_CONTINUE_PATH: Final = "/login/continue"
LOGOUT_PATH: Final = "/logout"
RESTORE_PATH: Final = "/restore"
PROGRESS_PREFIX: Final = "/restore/progress/"
STATIC_PREFIX: Final = "/static/"


class RestoreGate:
    """The instance phase under a restore or re-key, and the requests admitted so far."""

    def __init__(self) -> None:
        self.phase: GatePhase = "open"
        self.restore_id: str | None = None
        self.started_at: datetime | None = None
        self._purpose: GatePurpose | None = None
        self.failed_paths: tuple[str, ...] = ()
        self.automation_paused = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def banner_message(self) -> str | None:
        """Return the notice every page shows in this phase, or None when there is none."""
        match self.phase:
            case "engaged":
                return REKEY_MESSAGE if self._purpose == "rekey" else OFFLINE_MESSAGE
            case "swapping":
                return SWAPPING_MESSAGE
            case "failed":
                return FAILED_MESSAGE
            case _:
                return AUTOMATION_PAUSED_MESSAGE if self.automation_paused else None

    @property
    def purpose(self) -> GatePurpose | None:
        """Return what engaged the gate, or None while it is open."""
        return self._purpose

    @property
    def inflight(self) -> int:
        """Return how many admitted requests have not finished."""
        return self._inflight

    def engage(self, owner_id: str, at: datetime, *, purpose: GatePurpose) -> None:
        """Refuse mutating requests from now on."""
        self.phase = "engaged"
        self.restore_id = owner_id
        self.started_at = at
        self._purpose = purpose

    def begin_swap(self) -> None:
        """Refuse everything but the progress page, static files and liveness."""
        self.phase = "swapping"

    def reopen(self) -> None:
        """Admit requests again; a gate that failed stays failed until the process restarts."""
        if self.phase == "failed":
            return
        self.phase = "open"
        self.restore_id = None
        self.started_at = None
        self._purpose = None

    def hold_automation(self) -> None:
        """Note that the scheduler stays suspended until an administrator resumes it."""
        self.automation_paused = True

    def release_automation(self) -> None:
        """Note that the scheduler is no longer held."""
        self.automation_paused = False

    def mark_failed(self, paths: tuple[str, ...]) -> None:
        """Refuse every request and name the paths an operator needs."""
        self.phase = "failed"
        self.failed_paths = paths

    def enter(self) -> None:
        """Count an admitted request."""
        self._inflight += 1
        self._idle.clear()

    def leave(self) -> None:
        """Stop counting a finished request."""
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def drain(self, seconds: float) -> bool:
        """Wait up to the given seconds for admitted requests to finish; report whether they did."""
        if self._inflight == 0:
            return True
        try:
            async with asyncio.timeout(seconds):
                await self._idle.wait()
        except TimeoutError:
            return False
        return True


def admits(
    phase: GatePhase,
    method: str,
    path: str,
    *,
    websocket: bool = False,
    purpose: GatePurpose | None = None,
) -> bool:
    """Report whether a request is let through in this phase."""
    match phase:
        case "open":
            return True
        case "engaged":
            if websocket:
                return False
            if method in SAFE_METHODS or (
                method == "POST" and path in (LOGIN_PATH, LOGIN_CONTINUE_PATH, LOGOUT_PATH)
            ):
                return True
            return purpose != "rekey" and (
                path == RESTORE_PATH or path.startswith(f"{RESTORE_PATH}/")
            )
        case "swapping":
            return not websocket and _always_served(method, path)
        case _:
            return False


def _always_served(method: str, path: str) -> bool:
    return (
        path == HEALTH_PATH
        or path.startswith(STATIC_PREFIX)
        or (method in ("GET", "HEAD") and path.startswith(PROGRESS_PREFIX))
    )


def _message(gate: RestoreGate) -> str:
    return gate.banner_message or OFFLINE_MESSAGE


def _refusal(request: Request, gate: RestoreGate) -> Response:
    message = _message(gate)
    headers = {"Retry-After": RETRY_AFTER.get(gate.phase, "30")}
    paths = list(gate.failed_paths)
    if is_api_path(request):
        error = {"status": 503, "message": message}
        return no_store(JSONResponse({"error": error}, status_code=503, headers=headers))
    try:
        if is_htmx_request(request):
            return no_store(
                render_partial(
                    request,
                    "offline_notice",
                    message=message,
                    detail="\n".join(paths) or None,
                    status_code=503,
                    headers=headers,
                )
            )
        context = {"message": message, "phase": gate.phase, "purpose": gate.purpose, "paths": paths}
        return no_store(
            render_page(request, "restore/offline.html", context, status_code=503, headers=headers)
        )
    except Exception as error:
        logger.error("offline_page_render_failed", extra={"error": type(error).__name__})
        return no_store(PlainTextResponse(message, status_code=503, headers=headers))


class RestoreGateMiddleware:
    """Pure ASGI middleware that applies the gate to HTTP and WebSocket scopes before routing."""

    def __init__(self, app: ASGIApp, *, gate: RestoreGate | None = None) -> None:
        self.app = app
        self._gate = gate

    def _gate_of(self, scope: Scope) -> RestoreGate | None:
        if self._gate is not None:
            return self._gate
        application = scope.get("app")
        found: RestoreGate | None = getattr(
            getattr(application, "state", None), "restore_gate", None
        )
        return found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, or answer it with the offline refusal."""
        kind = scope["type"]
        gate = self._gate_of(scope)
        if gate is None or kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        websocket = kind == "websocket"
        method = "GET" if websocket else scope["method"]
        path = scope["path"]
        if not admits(gate.phase, method, path, websocket=websocket, purpose=gate.purpose):
            if websocket:
                await receive()
                await send({"type": "websocket.close", "code": CLOSE_TRY_AGAIN_LATER})
                return
            response = _refusal(Request(scope, receive), gate)
            await response(scope, receive, send)
            return
        if websocket:
            await self.app(scope, receive, send)
            return
        gate.enter()
        try:
            await self.app(scope, receive, send)
        finally:
            gate.leave()
