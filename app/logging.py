"""Structured JSON logging with per-request ids and secret scrubbing."""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final, TextIO

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.token_format import WEBHOOK_TOKEN_MARK, token_remnant_pattern
from app.paths import HEALTH_PATH, READY_PATH

REDACTED = "[REDACTED]"
REQUEST_ID_HEADER = "x-request-id"
LOG_INTERVAL_SECONDS = 60.0
WEBSOCKETS_PACKAGE_DIRECTORY = f"{os.sep}websockets{os.sep}"
PINNED_WEBSOCKET_LOGGERS = (
    "websockets",
    "websockets.server",
    "websockets.protocol",
    "uvicorn.asgi",
)
PINNED_CLIENT_LOGGERS = ("httpx", "httpcore")
UVICORN_ERROR_LOGGER = "uvicorn.error"
UVICORN_SERVER_NAME = "uvicorn.server"
ACCESS_LOGGER = "dockmin.access"
ACCESS_PATH_LIMIT = 1024
ACCESS_SKIPPED_PATHS = frozenset({HEALTH_PATH, READY_PATH})
RESTORE_PROGRESS_PREFIX = "/restore/progress/"
HTTP_REFUSAL_FLOOR = 400
WEBSOCKET_ACCEPTED_STATUS = 101
WEBSOCKET_REFUSED_STATUS = 403
WEBSOCKET_NORMAL_CLOSE = 1000
WEBSOCKET_NO_STATUS_CODE = 1005
WEBSOCKET_INTERNAL_ERROR = 1011
_CONNECTION_SCOPES = frozenset({"http", "websocket"})
_UNLOGGED_EXTRAS = frozenset({"color_message"})

SENSITIVE_SUFFIXES = (
    "_KEY",
    "_SECRET",
    "_PASSWORD",
    "_TOKEN",
    "_CREDENTIALS",
    "_CIPHERTEXT",
    "_PLAINTEXT",
    "_AUTH",
    "_PEM",
    "_AUTHORIZATION",
)
EXTRA_SENSITIVE_NAMES = frozenset(
    {
        "PASSWORD",
        "PASSWD",
        "PWD",
        "PASS",
        "PASSPHRASE",
        "SECRET",
        "TOKEN",
        "IDENTITYTOKEN",
        "REGISTRYTOKEN",
        "APIKEY",
        "AUTH",
        "CREDENTIALS",
        "CIPHERTEXT",
        "PLAINTEXT",
        "AUTHORIZATION",
        "PROXY_AUTHORIZATION",
        "COOKIE",
        "SET_COOKIE",
        "X_REGISTRY_AUTH",
        "REGISTRY_AUTH",
        "TLS_MATERIAL",
        # AWS field names are camelCase, so normalisation leaves no underscore for a suffix rule.
        "AUTHORIZATIONTOKEN",
        "SECRETACCESSKEY",
        "SESSIONTOKEN",
        # Unencrypted JSON column that is validated to hold no secret; redacted regardless.
        "PROVIDER_EXTRAS",
        # Free-form parameter bags are not validated for secrets, so they are always redacted.
        "PARAMETERS",
    }
)

# Traefik keeps htpasswd credentials under these label segments.
SENSITIVE_LABEL_SEGMENTS = frozenset({"BASICAUTH", "DIGESTAUTH"})

REQUEST_ID_RULE: Final = "1 to 64 letters, digits, dots, hyphens or underscores"
REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Lookbehind and bound confine each attempt to one token; without them every offset inside a
# long token retries the whole run, which is quadratic.
_NAME_AND_SEPARATOR = re.compile(
    r"""(?<![\w.\-])(?P<name>[\w.\-]{1,1024})(?P<sep>\\?["']?\s*[:=]\s*)"""
)

# A quoted value may hold backslash escapes, and an unterminated one runs to the end of the
# text. The \" form is a quoted string inside JSON that was itself serialised into a string.
_VALUE = re.compile(
    r"""
        "(?:[^"\\]|\\[\s\S]?)*(?:"|\Z)
        | '(?:[^'\\]|\\[\s\S]?)*(?:'|\Z)
        | \\"(?:[^\\]|\\(?!"))*(?:\\"|\Z)
        | (?:(?:Bearer|Basic)\s+)?[^\s,;&}\])"']+
    """,
    re.VERBOSE | re.IGNORECASE,
)

_URL_CREDENTIALS = re.compile(
    r"""(?P<head>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s:/?#@"']*:)[^\s/?#"']+(?=@)"""
)

# Tokens made only of letters must be long, so that "Bearer authentication" is left alone.
_BEARER_TOKEN = re.compile(
    r"""
        (?<![\w])(?P<scheme>Bearer)[ \t]+
        (?:(?=[A-Za-z]*[\d_.~+/=\-])[\w.~+/=\-]{8,} | [A-Za-z]{20,})
    """,
    re.VERBOSE | re.IGNORECASE,
)

_WEBHOOK_TOKEN = token_remnant_pattern(WEBHOOK_TOKEN_MARK)

# A compact JWS: three base64url segments, the first the encoding of a JSON object.
_COMPACT_JWS = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*(?![A-Za-z0-9_-])"
)

# An unterminated block runs to the end of the text: a truncated key is still a key, and a
# lazy scan that looked for a missing terminator from every header would be quadratic.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_STANDARD_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys() | {"message", "asctime", "taskName"}
)


class Scrubber:
    """Redact values stored under sensitive field names, and registered key material anywhere."""

    def __init__(self, extra_names: Collection[str] = ()) -> None:
        self._names = EXTRA_SENSITIVE_NAMES | {self._normalise(n) for n in extra_names}
        self._secret_values: tuple[str, ...] = ()
        self._secret_lock = threading.Lock()

    def register_secret(self, value: str) -> None:
        """Redact value wherever it appears in text from now on, whatever field holds it.

        For key material only: redacting a short user-chosen value by content would corrupt
        unrelated log lines.
        """
        if not value:
            return
        with self._secret_lock:
            if value in self._secret_values:
                return
            # Longest first, so a secret that contains another is replaced whole.
            self._secret_values = tuple(
                sorted((*self._secret_values, value), key=len, reverse=True)
            )

    def _redact_values(self, text: str) -> str:
        for secret in self._secret_values:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    @staticmethod
    def _normalise(name: str) -> str:
        return name.strip().upper().replace("-", "_").replace(".", "_")

    def is_sensitive(self, name: str) -> bool:
        """Report whether a field name designates a secret."""
        normalised = self._normalise(name)
        return normalised in self._names or normalised.endswith(SENSITIVE_SUFFIXES)

    def is_sensitive_label(self, key: str) -> bool:
        """Report whether the label key, or any dot-separated segment of it, designates a secret."""
        return self.is_sensitive(key) or any(
            self.is_sensitive(segment) or self._normalise(segment) in SENSITIVE_LABEL_SEGMENTS
            for segment in key.split(".")
        )

    def scrub_text(self, text: str) -> str:
        """Redact registered values, private keys, URL passwords, tokens and secret pairs."""
        text = _PRIVATE_KEY_BLOCK.sub(REDACTED, self._redact_values(text))
        text = _URL_CREDENTIALS.sub(lambda m: f"{m['head']}{REDACTED}", text)
        parts: list[str] = []
        copied = pos = 0
        # Scanning resumes right after each separator, so a pair inside a non-sensitive value
        # ("Error: password=...") is still found, without recursing once per nested pair.
        while (match := _NAME_AND_SEPARATOR.search(text, pos)) is not None:
            pos = match.end()
            if not self.is_sensitive(match["name"]):
                continue
            value = _VALUE.match(text, pos)
            if value is None:
                continue
            parts.append(text[copied:pos])
            parts.append(REDACTED)
            copied = pos = value.end()
        parts.append(text[copied:])
        text = _BEARER_TOKEN.sub(lambda m: f"{m['scheme']} {REDACTED}", "".join(parts))
        text = _WEBHOOK_TOKEN.sub(lambda m: f"{WEBHOOK_TOKEN_MARK}_{m['prefix']}_{REDACTED}", text)
        return _COMPACT_JWS.sub(REDACTED, text)

    def scrub(self, value: Any) -> Any:
        """Return a copy of value with every sensitive entry redacted, recursively."""
        if isinstance(value, Mapping):
            return {
                str(k): REDACTED if self.is_sensitive(str(k)) else self.scrub(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [self.scrub(item) for item in value]
        if isinstance(value, str):
            return self.scrub_text(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        return self.scrub_text(str(value))


DEFAULT_SCRUBBER = Scrubber()


class WebSocketPayloadFilter(logging.Filter):
    """Drop debug records raised inside a websockets package, whichever logger carries them.

    The library logs frame contents through a logger the server chooses, so filtering by logger
    name misses them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep a record unless it is a debug record from a websockets package."""
        return record.levelno > logging.DEBUG or WEBSOCKETS_PACKAGE_DIRECTORY not in record.pathname


def _escape_ansi(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\x1b", "\\u001b").replace("\x9b", "\\u009b")
    return value


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON objects, scrubbed before serialisation."""

    def __init__(self, scrubber: Scrubber = DEFAULT_SCRUBBER) -> None:
        super().__init__()
        self._scrubber = scrubber

    def format(self, record: logging.LogRecord) -> str:
        """Render one record as JSON."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": UVICORN_SERVER_NAME if record.name == UVICORN_ERROR_LOGGER else record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(
            {
                k: _escape_ansi(v)
                for k, v in record.__dict__.items()
                if k not in _STANDARD_RECORD_FIELDS and k not in _UNLOGGED_EXTRAS
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(self._scrubber.scrub(payload), ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    stream: TextIO | None = None,
    scrubber: Scrubber = DEFAULT_SCRUBBER,
) -> None:
    """Route all logging, including uvicorn's, through the JSON formatter on stdout."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(scrubber))
    handler.set_name("dockmin-json")
    handler.addFilter(WebSocketPayloadFilter())

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == "dockmin-json"]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; leaving them in place would emit unscrubbed text.
    for name in ("uvicorn", UVICORN_ERROR_LOGGER, "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # uvicorn gives this logger to the websockets library, so frame records are raised on it.
    error_logger = logging.getLogger(UVICORN_ERROR_LOGGER)
    for stale in [f for f in error_logger.filters if isinstance(f, WebSocketPayloadFilter)]:
        error_logger.removeFilter(stale)
    error_logger.addFilter(WebSocketPayloadFilter())

    # At DEBUG these log frame contents, which on a terminal socket is what the user typed.
    for name in PINNED_WEBSOCKET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))

    # At INFO httpx logs each request's full URL, and a URL can carry a credential.
    for name in PINNED_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    """Return a logger by name."""
    return logging.getLogger(name)


@contextmanager
def bound_request_id(request_id: str) -> Iterator[None]:
    """Attach a request id to every record emitted inside the block."""
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


class RequestIdMiddleware:
    """Bind a request id for each HTTP request and WebSocket connection; HTTP responses echo it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the id, then delegate to the wrapped application."""
        kind = scope["type"]
        if kind not in _CONNECTION_SCOPES:
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        request_id = incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else uuid.uuid4().hex
        # Outer error middleware renders unhandled-exception responses after this
        # middleware has unwound, so the id is also kept on the shared scope state.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with bound_request_id(request_id):
            await self.app(scope, receive, send_with_id if kind == "http" else send)


class AccessLogMiddleware:
    """Write one line per HTTP request and per WebSocket connection, once it has ended.

    A `listener` name is added to every line. With `refusals_only`, requests answered below 400
    and accepted connections write nothing.
    """

    def __init__(
        self, app: ASGIApp, *, listener: str | None = None, refusals_only: bool = False
    ) -> None:
        self.app = app
        self._listener = listener
        self._refusals_only = refusals_only
        self._logger = get_logger(ACCESS_LOGGER)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Delegate to the wrapped application, then log the request or connection."""
        kind = scope["type"]
        if kind not in _CONNECTION_SCOPES or scope["path"] in ACCESS_SKIPPED_PATHS:
            await self.app(scope, receive, send)
        elif kind == "http":
            await self._serve_http(scope, receive, send)
        else:
            await self._serve_websocket(scope, receive, send)

    async def _serve_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        status = 500

        async def send_recording(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_recording)
        finally:
            if not self._refusals_only or status >= HTTP_REFUSAL_FLOOR:
                self._write(
                    "http_request",
                    {
                        "method": scope["method"],
                        "path": _logged_path(scope["path"]),
                        "status": status,
                        "duration_ms": _elapsed_ms(started),
                        **_caller(scope),
                    },
                )

    async def _serve_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        accepted = False
        refusal_status: int | None = None
        close_code: int | None = None
        completed = False

        async def receive_recording() -> Message:
            nonlocal close_code
            message = await receive()
            if message["type"] == "websocket.disconnect" and close_code is None:
                close_code = message.get("code", WEBSOCKET_NO_STATUS_CODE)
            return message

        async def send_recording(message: Message) -> None:
            nonlocal accepted, refusal_status, close_code
            match message["type"]:
                case "websocket.accept":
                    accepted = True
                case "websocket.close":
                    if close_code is None:
                        close_code = message.get("code", WEBSOCKET_NORMAL_CLOSE)
                    if not accepted and refusal_status is None:
                        refusal_status = WEBSOCKET_REFUSED_STATUS
                case "websocket.http.response.start":
                    refusal_status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_recording, send_recording)
            completed = True
        finally:
            if accepted and close_code is None:
                close_code = WEBSOCKET_NORMAL_CLOSE if completed else WEBSOCKET_INTERNAL_ERROR
            if not (self._refusals_only and accepted):
                status = WEBSOCKET_ACCEPTED_STATUS if accepted else refusal_status
                self._write(
                    "websocket_connection",
                    {
                        "path": _logged_path(scope["path"]),
                        "status": 500 if status is None else status,
                        "close_code": close_code,
                        "duration_ms": _elapsed_ms(started),
                        **_caller(scope),
                    },
                )

    def _write(self, event: str, fields: dict[str, Any]) -> None:
        if self._listener is not None:
            fields["listener"] = self._listener
        self._logger.info(event, extra=fields)


def _logged_path(path: str) -> str:
    if len(path) > ACCESS_PATH_LIMIT:
        path = f"{path[:ACCESS_PATH_LIMIT]}\u2026"
    if path.startswith(RESTORE_PROGRESS_PREFIX):
        return f"{RESTORE_PROGRESS_PREFIX}{REDACTED}"
    return path


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _caller(scope: Scope) -> dict[str, Any]:
    client = scope.get("client")
    return {
        "address": client[0] if client else "unknown",
        "user_id": scope.get("state", {}).get("authenticated_user_id"),
    }
