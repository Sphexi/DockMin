"""Recognising a bearer-token request, which decides both who it is and whether CSRF applies."""

from fastapi import Request
from starlette.requests import HTTPConnection

_BEARER_SCHEME = "bearer"


def is_api_path(request: HTTPConnection) -> bool:
    """Report whether the request addresses the JSON surface."""
    path = request.url.path
    return path == "/api" or path.startswith("/api/")


def bearer_credentials(request: Request) -> str | None:
    """Return the presented token of a bearer request to the JSON surface, otherwise None.

    A request that carries the header is a bearer request even when the value is empty or wrong:
    it is authenticated by the token or refused, and its cookies are never consulted. That is the
    property that makes skipping CSRF for it safe, since a cookie is the only thing CSRF abuses.
    The header is ignored off the JSON surface.

    Every state-changing /api route must authenticate: the CSRF exemption assumes it, and
    nothing here enforces it.
    """
    if not is_api_path(request):
        return None
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != _BEARER_SCHEME:
        return None
    return value.strip()


def is_bearer_request(request: Request) -> bool:
    """Report whether the request is authenticated, or refused, by a bearer token alone."""
    return bearer_credentials(request) is not None
