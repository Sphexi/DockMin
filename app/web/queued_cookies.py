"""Cookies set before a route builds its response, attached to whatever response it returns."""

from collections.abc import Callable
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATE_KEY: Final = "queued_cookies"


def queue_cookie(connection: HTTPConnection, apply: Callable[[Response], None]) -> None:
    """Have the response to this request carry the cookie `apply` sets on a scratch response."""
    scratch = Response()
    apply(scratch)
    queued: list[bytes] = connection.scope.setdefault("state", {}).setdefault(STATE_KEY, [])
    queued.extend(value for name, value in scratch.raw_headers if name == b"set-cookie")


class QueuedCookiesMiddleware:
    """Add each queued cookie to the response start of the request that queued it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap the response start so queued cookies are appended to it."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookies(message: Message) -> None:
            if message["type"] == "http.response.start":
                queued = scope.get("state", {}).get(STATE_KEY)
                if queued:
                    headers = MutableHeaders(scope=message)
                    for value in queued:
                        headers.append("set-cookie", value.decode("latin-1"))
            await send(message)

        await self.app(scope, receive, send_with_cookies)
