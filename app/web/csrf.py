"""CSRF token issue, lookup and enforcement for every mutating request."""

import hmac
import re
import secrets
from typing import Final
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocketException
from starlette.requests import HTTPConnection
from starlette.responses import Response

from app.auth.bearer import is_bearer_request
from app.config import Settings
from app.paths import HOOKS_PATH

# The __Host- prefix makes a browser refuse a copy set by a sibling subdomain or over plaintext. It
# requires the Secure attribute, so a deployment that disables Secure falls back to the bare name.
CSRF_COOKIE = "__Host-dockmin_csrf"
INSECURE_CSRF_COOKIE = "dockmin_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
# Routes whose body is read as a stream by the route itself, which then checks the token from the
# first multipart part. Keyed on the exact method and path.
STREAMED_UPLOAD_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {("POST", "/restore/archive")}
)
# Streamed upload routes whose path carries identifiers, with a body cap of their own.
CONTEXT_UPLOAD_PATH: Final = re.compile(
    r"/environments/[1-9][0-9]{0,18}/stacks/[a-z0-9][a-z0-9_-]{0,62}/contexts/-/upload"
)
# Webhook calls authenticate by a secret in the URL or the Authorization header and read no cookie.
TOKEN_AUTHENTICATED_PATH: Final = re.compile(rf"{re.escape(HOOKS_PATH)}(/[^/]*)?")


def is_context_upload(method: str, path: str) -> bool:
    """Report whether the request is the build context upload of a stack."""
    return method == "POST" and CONTEXT_UPLOAD_PATH.fullmatch(path) is not None


def is_streamed_upload(method: str, path: str) -> bool:
    """Report whether the route reads its own body and checks the token from its first part."""
    return (method, path) in STREAMED_UPLOAD_ROUTES or is_context_upload(method, path)


def is_webhook_call(connection: HTTPConnection) -> bool:
    """Report whether the request is a POST to a webhook path, which carries its own secret."""
    return (
        isinstance(connection, Request)
        and connection.method == "POST"
        and TOKEN_AUTHENTICATED_PATH.fullmatch(connection.scope["path"]) is not None
    )


def csrf_cookie_name(settings: Settings) -> str:
    """Return the name the CSRF cookie carries under the configured Secure setting."""
    return CSRF_COOKIE if settings.cookie_secure else INSECURE_CSRF_COOKIE


def new_token() -> str:
    """Return a fresh 256-bit URL-safe token."""
    return secrets.token_urlsafe(32)


def _settings(connection: HTTPConnection) -> Settings:
    settings: Settings = connection.app.state.settings
    return settings


def csrf_token(request: Request) -> str:
    """Return this request's token, reusing the cookie's value when it is well formed."""
    cached: str | None = getattr(request.state, "csrf_token", None)
    if cached is not None:
        return cached
    presented = request.cookies.get(csrf_cookie_name(_settings(request)), "")
    if _TOKEN_PATTERN.fullmatch(presented):
        token = presented
    else:
        token = new_token()
        request.state.csrf_issued = True
    request.state.csrf_token = token
    return token


def attach_csrf_cookie(request: Request, response: Response) -> None:
    """Set the cookie when this request minted a token."""
    if not getattr(request.state, "csrf_issued", False):
        return
    settings = _settings(request)
    response.set_cookie(
        csrf_cookie_name(settings),
        request.state.csrf_token,
        # The token reaches the page through the template, so script never needs the cookie.
        httponly=True,
        samesite="lax",
        # The configured value, not the scheme this process saw: behind a TLS-terminating proxy
        # the process sees http, and the session cookie follows the configuration too.
        secure=settings.cookie_secure,
        path="/",
    )


def clear_csrf_cookie(response: Response, settings: Settings) -> None:
    """Instruct the browser to drop the CSRF cookie."""
    response.delete_cookie(
        csrf_cookie_name(settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _is_form_type(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in FORM_TYPES


def _form_declared(request: Request) -> bool:
    return _is_form_type(request.headers.get("content-type", ""))


async def _presented_token(request: Request) -> str | None:
    header = request.headers.get(CSRF_HEADER)
    if header is not None:
        return header
    if not _form_declared(request):
        return None
    value = (await request.form()).get(CSRF_FORM_FIELD)
    return value if isinstance(value, str) else None


def _origin_is_acceptable(
    connection: HTTPConnection, trusted_origins: list[str], *, allow_referer: bool = True
) -> bool:
    """Accept a request whose Origin (or allowed Referer) names this host or a trusted origin."""
    source = connection.headers.get("origin")
    if source is None:
        referer = connection.headers.get("referer") if allow_referer else None
        if referer is None:
            # A browser sends Origin on every cross-site mutating request, so a caller that sends
            # neither is not one this check can vouch for.
            return False
        parts = urlsplit(referer)
        source = f"{parts.scheme}://{parts.netloc}"
    if source.lower() in trusted_origins:
        return True
    # Compared on host only: behind a TLS-terminating proxy this process sees http while the
    # browser sends an https origin. An empty host never matches, so "null" cannot pass a request
    # that carries no Host header.
    netloc = urlsplit(source).netloc.lower()
    return bool(netloc) and netloc == connection.headers.get("host", "").lower()


def verify_presented_token(request: Request, presented: str | None) -> None:
    """Raise a 403 unless the presented token equals the cookie's and the Origin is acceptable."""
    settings = _settings(request)
    cookie = request.cookies.get(csrf_cookie_name(settings), "")
    if (
        not _TOKEN_PATTERN.fullmatch(cookie)
        or presented is None
        or not hmac.compare_digest(cookie.encode("utf-8"), presented.encode("utf-8"))
        or not _origin_is_acceptable(request, settings.trusted_origins)
    ):
        raise HTTPException(status_code=403)


async def enforce_csrf(connection: HTTPConnection) -> None:
    """Refuse a mutating request unless it carries the cookie's token in the header or the form.

    Must stay an application-wide dependency so every route is covered. Exempt: a bearer request
    to the JSON surface, whose cookies are never consulted; a streamed upload route, which checks
    the token itself before reading any file byte, so the form is not parsed here; a webhook call,
    by path, which reads no cookie.

    A WebSocket handshake is held to the Origin rule alone, without the Referer fallback, and is
    refused before it is accepted.
    """
    if not isinstance(connection, Request):
        if not _origin_is_acceptable(
            connection, _settings(connection).trusted_origins, allow_referer=False
        ):
            raise WebSocketException(code=1008)
        return
    request = connection
    if request.method in SAFE_METHODS or is_bearer_request(request):
        return
    if is_streamed_upload(request.method, request.scope["path"]):
        return
    if is_webhook_call(request):
        return
    verify_presented_token(request, await _presented_token(request))
