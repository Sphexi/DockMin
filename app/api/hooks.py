"""Inbound webhook calls: a token in the path or in the Authorization header starts one run."""

from http import HTTPStatus
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.auth.bearer import bearer_credentials
from app.auth.dependencies import remote_address
from app.db.engine import Session, get_session
from app.paths import HOOKS_PATH
from app.services.assembly import build_webhook_service
from app.services.webhooks import CallResult, Outcome, WebhookService
from app.tls import served_over_tls

router = APIRouter(prefix=HOOKS_PATH, tags=["hooks"])

NO_STORE: Final = {"Cache-Control": "no-store"}
_REFUSALS: Final = {
    Outcome.USE_HTTPS: HTTPStatus.BAD_REQUEST,
    Outcome.QUERY_NOT_SUPPORTED: HTTPStatus.BAD_REQUEST,
    Outcome.NOT_ACCEPTING: HTTPStatus.CONFLICT,
}


def _service(request: Request, db: Session) -> WebhookService:
    state = request.app.state
    return build_webhook_service(
        db,
        state.auth,
        getattr(state, "scheduler", None),
        limiter=state.webhook_limiter,
        tls_enabled=state.settings.tls_enabled,
    )


def _answer(result: CallResult) -> Response:
    outcome = result.outcome
    if outcome in (Outcome.ACCEPTED, Outcome.ALREADY_PENDING):
        return JSONResponse({"status": outcome.value}, status_code=202, headers=NO_STORE)
    if outcome is Outcome.NOT_FOUND:
        # No extra header: this answer must equal the one for an unknown /api path.
        raise HTTPException(status_code=404)
    if outcome is Outcome.RATE_LIMITED:
        raise HTTPException(
            status_code=429, headers={**NO_STORE, "Retry-After": str(result.retry_after)}
        )
    status = _REFUSALS[outcome]
    reason = result.reason or outcome.value
    error = {"status": status.value, "message": status.phrase, "reason": reason}
    return JSONResponse({"error": error}, status_code=status.value, headers=NO_STORE)


async def _call(request: Request, db: Session, presented: str | None) -> Response:
    over_tls = served_over_tls(request) or request.url.scheme == "https"
    result = await _service(request, db).call(
        presented,
        address=remote_address(request),
        over_tls=over_tls,
        has_query=bool(request.scope.get("query_string")),
    )
    return _answer(result)


@router.post("/{token}", include_in_schema=True)
async def call_by_path(
    token: str, request: Request, db: Annotated[Session, Depends(get_session)]
) -> Response:
    """Start the webhook's run; the token is the last path segment."""
    return await _call(request, db, token)


@router.post("", include_in_schema=True)
async def call_by_header(
    request: Request, db: Annotated[Session, Depends(get_session)]
) -> Response:
    """Start the webhook's run; the token is the bearer credential."""
    return await _call(request, db, bearer_credentials(request))
