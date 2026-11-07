"""Reading the notices a redirecting post leaves for the page it lands on."""

import re
from html import unescape
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from httpx import AsyncClient, Response
from starlette.responses import Response as StarletteResponse

from app.auth.sessions import session_cookie_name
from app.config import Settings
from app.web.flash import (
    FLASH_COOKIE,
    INSECURE_FLASH_COOKIE,
    Flash,
    flash_cookie_name,
    write_flashes,
)

SEE_OTHER = 303
NO_CONTENT = 204

_TOAST = re.compile(
    r'<div class="toast" data-toast data-level="(\w+)" role="\w+">\s*'
    r'<span class="toast__message" data-toast-message>(.*?)</span>',
    re.DOTALL,
)


class Ticker:
    """A clock that moves only when told to; calling it returns seconds."""

    def __init__(self, start: float = 1_900_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def location(response: Response) -> str:
    """Return a response's Location header."""
    return response.headers["location"]


def flashes_in(html: str) -> list[tuple[str, str]]:
    """Return the (level, message) of each notice a full page renders."""
    return [(level, unescape(message)) for level, message in _TOAST.findall(html) if message]


def flash_texts(html: str) -> list[str]:
    """Return the message of each notice a full page renders."""
    return [message for _, message in flashes_in(html)]


def flash_cookie_of(client: AsyncClient) -> str | None:
    """Return the notice cookie the client holds under either name, or None."""
    for name in (FLASH_COOKIE, INSECURE_FLASH_COOKIE):
        if name in client.cookies:
            return client.cookies[name]
    return None


async def land(client: AsyncClient, response: Response) -> Response:
    """Follow a 303 once, as a browser would, and return the page it leads to."""
    assert response.status_code == SEE_OTHER, response.text
    return await client.get(location(response))


async def landed_texts(client: AsyncClient, response: Response) -> list[str]:
    """Follow a 303 and return the notices the landing page shows."""
    return flash_texts((await land(client, response)).text)


def fake_app(settings: Settings, key: bytes, clock: Ticker) -> Any:
    """Return just enough of an application for the notice functions: key, settings and clock."""
    return SimpleNamespace(
        state=SimpleNamespace(flash_key=key, settings=settings, flash_clock=clock)
    )


def request_with(app: Any, cookies: dict[str, str] | None = None) -> Request:
    """Build a request to the application carrying the given cookies."""
    header = "; ".join(f"{name}={value}" for name, value in (cookies or {}).items())
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(b"cookie", header.encode("latin-1"))] if header else [],
        "app": app,
    }
    return Request(scope)


def issue_flash(
    app: Any, flashes: list[Flash], *, session_token: str = ""
) -> tuple[StarletteResponse, str]:
    """Write notices as a request holding the session token would; return response and value."""
    settings: Settings = app.state.settings
    cookies = {session_cookie_name(settings): session_token} if session_token else {}
    response = StarletteResponse()
    write_flashes(request_with(app, cookies), response, flashes)
    jar: SimpleCookie = SimpleCookie()
    jar.load(response.headers["set-cookie"])
    return response, jar[flash_cookie_name(settings)].value
