"""Signed one-shot notices carried by a cookie from a redirecting post to the page it lands on."""

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from fastapi import Request
from starlette.responses import Response

from app.auth.sessions import session_cookie_name
from app.config import Settings

type FlashLevel = Literal["info", "ok", "warn", "danger"]

FLASH_COOKIE: Final = "__Host-dockmin_flash"
INSECURE_FLASH_COOKIE: Final = "dockmin_flash"
MAX_FLASHES: Final = 3
MAX_FLASH_CHARS: Final = 500
FLASH_MAX_AGE: Final = 120
FLASH_KEY_BYTES: Final = 32
BINDING_CHARS: Final = 16
MAX_COOKIE_CHARS: Final = 3500
_LEVELS: Final[dict[str, FlashLevel]] = {
    "info": "info",
    "ok": "ok",
    "warn": "warn",
    "danger": "danger",
}
_PENDING_ATTRIBUTE: Final = "flash_pending"


@dataclass(frozen=True, slots=True)
class Flash:
    """One notice: how it is toned and what it says."""

    level: FlashLevel
    message: str


def _key(request: Request) -> bytes | None:
    key: bytes | None = getattr(request.app.state, "flash_key", None)
    return key


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _clock(request: Request) -> float:
    clock: Callable[[], float] = getattr(request.app.state, "flash_clock", time.time)
    return clock()


def flash_cookie_name(settings: Settings) -> str:
    """Return the name the notice cookie carries under the configured Secure setting."""
    return FLASH_COOKIE if settings.cookie_secure else INSECURE_FLASH_COOKIE


def _binding(request: Request) -> str:
    """Return a short digest of the request's session token; empty before sign-in."""
    token = request.cookies.get(session_cookie_name(_settings(request)), "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:BINDING_CHARS] if token else ""


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def _encode(key: bytes, flashes: Sequence[Flash], *, issued: int, binding: str) -> str:
    """Return the cookie value: the payload and its signature, shrunk until it fits a cookie."""
    kept = [Flash(flash.level, flash.message[:MAX_FLASH_CHARS]) for flash in flashes[-MAX_FLASHES:]]
    while True:
        document = json.dumps(
            {
                "iat": issued,
                "sid": binding,
                "flashes": [{"level": f.level, "message": f.message} for f in kept],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = base64.urlsafe_b64encode(document.encode("utf-8")).decode("ascii").rstrip("=")
        value = f"{payload}.{_sign(key, payload)}"
        if len(value) <= MAX_COOKIE_CHARS or not kept:
            return value
        if len(kept) > 1:
            kept = kept[1:]
        else:
            last = kept[0]
            kept = [Flash(last.level, last.message[: len(last.message) // 2])]


def _decode(key: bytes, value: str, *, now: float, binding: str) -> tuple[Flash, ...]:
    if len(value) > MAX_COOKIE_CHARS:
        return ()
    payload, separator, signature = value.partition(".")
    if not separator or not payload.isascii():
        return ()
    if not hmac.compare_digest(signature.encode("ascii", "replace"), _sign(key, payload).encode()):
        return ()
    try:
        padded = payload + "=" * (-len(payload) % 4)
        document: Any = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, ValueError):
        return ()
    if not isinstance(document, dict):
        return ()
    issued, bound, entries = document.get("iat"), document.get("sid"), document.get("flashes")
    if not isinstance(issued, int) or isinstance(issued, bool) or now - issued > FLASH_MAX_AGE:
        return ()
    if not isinstance(bound, str) or not hmac.compare_digest(
        bound.encode("utf-8"), binding.encode("utf-8")
    ):
        return ()
    if not isinstance(entries, list) or len(entries) > MAX_FLASHES:
        return ()
    flashes: list[Flash] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return ()
        raw_level, message = entry.get("level"), entry.get("message")
        level = _LEVELS.get(raw_level) if isinstance(raw_level, str) else None
        if level is None or not isinstance(message, str) or len(message) > MAX_FLASH_CHARS:
            return ()
        flashes.append(Flash(level, message))
    return tuple(flashes)


def _cookie_attributes(request: Request) -> dict[str, Any]:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": _settings(request).cookie_secure,
        "path": "/",
    }


def write_flashes(request: Request, response: Response, flashes: Sequence[Flash]) -> None:
    """Attach the notices to a redirect; only the newest MAX_FLASHES are kept."""
    key = _key(request)
    if key is None or not flashes:
        return
    response.set_cookie(
        flash_cookie_name(_settings(request)),
        _encode(key, flashes, issued=int(_clock(request)), binding=_binding(request)),
        max_age=FLASH_MAX_AGE,
        **_cookie_attributes(request),
    )


def pending_flashes(request: Request) -> tuple[Flash, ...]:
    """Return the verified notices this request carries; an unverifiable cookie yields none."""
    cached: tuple[Flash, ...] | None = getattr(request.state, _PENDING_ATTRIBUTE, None)
    if cached is not None:
        return cached
    key = _key(request)
    value = request.cookies.get(flash_cookie_name(_settings(request)))
    flashes = (
        ()
        if key is None or value is None
        else _decode(key, value, now=_clock(request), binding=_binding(request))
    )
    setattr(request.state, _PENDING_ATTRIBUTE, flashes)
    return flashes


def clear_flashes(request: Request, response: Response) -> None:
    """Expire the cookie on the response that displayed it, if the request carried one."""
    name = flash_cookie_name(_settings(request))
    if name in request.cookies:
        response.delete_cookie(name, **_cookie_attributes(request))
