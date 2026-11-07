"""Security response headers on every response, and the application class that guarantees it."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, cast

from fastapi import FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.selfcheck import StartupCheckFailed
from app.tls import TLS_SELF_SIGNED_SCOPE_KEY

type PolicyName = Literal["default", "terminal"]

_DEFAULT_POLICY: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
CONTENT_SECURITY_POLICIES: Final[Mapping[PolicyName, str]] = MappingProxyType(
    {
        "default": _DEFAULT_POLICY,
        "terminal": f"{_DEFAULT_POLICY}; style-src-elem 'self' 'unsafe-inline'",
    }
)
HSTS_VALUE: Final = "max-age=31536000"
POLICY_STATE_KEY: Final = "content_security_policy"
STARTUP_FAILURE_ATTRIBUTE: Final = "startup_failure"

_FIXED_HEADERS: Final = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def use_policy(request: Request, name: PolicyName) -> None:
    """Select the Content-Security-Policy the response to this request carries."""
    setattr(request.state, POLICY_STATE_KEY, name)


class SecurityHeadersMiddleware:
    """Set the security headers on every HTTP response, replacing any value a route set."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stamp the headers onto the response start message."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                chosen = scope.get("state", {}).get(POLICY_STATE_KEY)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICIES.get(
                    cast(PolicyName, chosen), _DEFAULT_POLICY
                )
                for name, value in _FIXED_HEADERS.items():
                    headers[name] = value
                if (
                    scope.get("scheme") == "https"
                    and scope.get(TLS_SELF_SIGNED_SCOPE_KEY) is not True
                ):
                    headers["Strict-Transport-Security"] = HSTS_VALUE
            await send(message)

        await self.app(scope, receive, send_with_headers)


class DockminApp(FastAPI):
    """The application: security headers outside every layer, and a quiet failed start.

    The headers wrap Starlette's outermost error layer, which renders the 500 response outside
    every middleware added with add_middleware. A start that fails a startup check has already
    logged its one line, so the traceback the lifespan would otherwise print is dropped.
    """

    def build_middleware_stack(self) -> ASGIApp:
        """Build the stack and wrap it in the security headers."""
        return SecurityHeadersMiddleware(super().build_middleware_stack())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve a request; on the lifespan scope, report a failed startup check without a trace."""
        if scope["type"] != "lifespan":
            await super().__call__(scope, receive, send)
            return

        if hasattr(self.state, STARTUP_FAILURE_ATTRIBUTE):
            delattr(self.state, STARTUP_FAILURE_ATTRIBUTE)

        async def quiet_send(message: Message) -> None:
            failed = getattr(self.state, STARTUP_FAILURE_ATTRIBUTE, None)
            if message["type"] == "lifespan.startup.failed" and failed is not None:
                message = {**message, "message": ""}
            await send(message)

        try:
            await super().__call__(scope, receive, quiet_send)
        except StartupCheckFailed as error:
            if getattr(self.state, STARTUP_FAILURE_ATTRIBUTE, None) is not error:
                raise
            delattr(self.state, STARTUP_FAILURE_ATTRIBUTE)
