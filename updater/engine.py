"""Minimal Docker Engine API client over a Unix socket, using only the standard library."""

import http.client
import json
import socket
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlencode

from updater.plan import API_VERSION_PATTERN, CONTAINER_ID_PATTERN

REQUEST_TIMEOUT_SECONDS = 30.0
STOP_GRACE_SECONDS = 30.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ERROR_BYTES = 4096
MAX_ERROR_CHARS = 300
LOG_TAIL_LINES = 200
MAX_LOG_LINE_BYTES = 1024
MAX_LOG_STREAM_BYTES = 16 * 1024 * 1024
LOG_READ_CHUNK = 64 * 1024
LOG_HEADER_BYTES = 8
STDERR_STREAM = 2


class EngineError(Exception):
    """A request failed or the Engine answered with an unexpected status or body."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class Engine:
    """Engine API calls the update sequence needs, one connection per call."""

    def __init__(self, socket_path: str, api_version: str) -> None:
        if API_VERSION_PATTERN.fullmatch(api_version) is None:
            raise ValueError("malformed API version")
        self._socket_path = socket_path
        self._prefix = f"/v{api_version}"

    def inspect_container(self, reference: str) -> dict[str, Any]:
        """Return the inspect document; a 64-hex reference must come back with that exact id."""
        info = self._object("GET", f"/containers/{quote(reference, safe='')}/json")
        if CONTAINER_ID_PATTERN.fullmatch(reference) and info.get("Id") != reference:
            raise EngineError("inspect returned a different container")
        return info

    def inspect_image(self, reference: str) -> dict[str, Any]:
        """Return the image inspect document."""
        return self._object("GET", f"/images/{quote(reference, safe='/:@')}/json")

    def stop_container(self, container_id: str, timeout_seconds: int) -> None:
        """Stop a container; an already stopped one is not an error."""
        self._empty(
            "POST",
            f"/containers/{container_id}/stop",
            {"t": str(timeout_seconds)},
            accept=(204, 304),
            timeout=timeout_seconds + STOP_GRACE_SECONDS,
        )

    def start_container(self, container_id: str) -> None:
        """Start a container; an already running one is not an error."""
        self._empty("POST", f"/containers/{container_id}/start", accept=(204, 304))

    def rename_container(self, container_id: str, name: str) -> None:
        """Rename a container."""
        self._empty("POST", f"/containers/{container_id}/rename", {"name": name}, accept=(204,))

    def remove_container(self, container_id: str, *, force: bool) -> None:
        """Remove a container without its volumes."""
        query = {"force": "1" if force else "0", "v": "0"}
        self._empty("DELETE", f"/containers/{container_id}", query, accept=(204,))

    def create_container(self, name: str, body: dict[str, Any]) -> str:
        """Create a container and return its id."""
        with self._stream(
            "POST",
            "/containers/create",
            {"name": name},
            body=body,
            accept=(201,),
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            created = _decode_object(_read(response, MAX_JSON_BYTES), "create")
        container_id = created.get("Id")
        if (
            not isinstance(container_id, str)
            or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
        ):
            raise EngineError("create returned a malformed id")
        return container_id

    def container_logs(self, container_id: str, *, tty: bool) -> list[str]:
        """Return the last log lines, each cut to 1 KiB, reading a bounded amount of the stream."""
        collector = _LineCollector()
        query = {"stdout": "1", "stderr": "1", "tail": str(LOG_TAIL_LINES)}
        with self._stream(
            "GET",
            f"/containers/{container_id}/logs",
            query,
            accept=(200,),
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            try:
                if tty:
                    _read_raw_logs(response, collector)
                else:
                    _read_framed_logs(response, collector)
            except (OSError, http.client.HTTPException) as exc:
                raise EngineError(f"reading logs failed: {exc}") from exc
        return collector.lines()

    def _object(
        self, method: str, path: str, query: dict[str, str] | None = None
    ) -> dict[str, Any]:
        with self._stream(
            method, path, query, accept=(200,), timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return _decode_object(_read(response, MAX_JSON_BYTES), path)

    def _empty(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        *,
        accept: tuple[int, ...],
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        with self._stream(method, path, query, accept=accept, timeout=timeout) as response:
            _read(response, MAX_JSON_BYTES)

    @contextmanager
    def _stream(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        *,
        body: dict[str, Any] | None = None,
        accept: tuple[int, ...],
        timeout: float,
    ) -> Iterator[http.client.HTTPResponse]:
        target = self._prefix + path + (f"?{urlencode(query)}" if query else "")
        headers = {"Connection": "close"}
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = _UnixConnection(self._socket_path, timeout)
        try:
            try:
                connection.request(method, target, body=payload, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                raise EngineError(f"{method} {path} failed: {exc}") from exc
            if response.status not in accept:
                raise EngineError(
                    f"{method} {path}: {response.status} {_error_message(response)}",
                    response.status,
                )
            yield response
        finally:
            connection.close()


def _read(response: http.client.HTTPResponse, limit: int) -> bytes:
    try:
        data = response.read(limit + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError(f"reading the response failed: {exc}") from exc
    if len(data) > limit:
        raise EngineError(f"response exceeds {limit} bytes")
    return data


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise EngineError(f"{label}: response is not JSON") from exc
    if not isinstance(value, dict):
        raise EngineError(f"{label}: response is not a JSON object")
    return value


def _error_message(response: http.client.HTTPResponse) -> str:
    try:
        raw = response.read(MAX_ERROR_BYTES)
    except (OSError, http.client.HTTPException):
        return ""
    text = raw.decode("utf-8", errors="replace")
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    if isinstance(decoded, dict) and isinstance(decoded.get("message"), str):
        text = decoded["message"]
    return " ".join(text.split())[:MAX_ERROR_CHARS]


def _read_raw_logs(response: http.client.HTTPResponse, collector: "_LineCollector") -> None:
    consumed = 0
    while consumed < MAX_LOG_STREAM_BYTES:
        chunk = response.read(LOG_READ_CHUNK)
        if not chunk:
            return
        consumed += len(chunk)
        collector.feed(1, chunk)


def _read_framed_logs(response: http.client.HTTPResponse, collector: "_LineCollector") -> None:
    consumed = 0
    while consumed < MAX_LOG_STREAM_BYTES:
        header = response.read(LOG_HEADER_BYTES)
        if len(header) < LOG_HEADER_BYTES:
            return
        stream = header[0]
        if stream > STDERR_STREAM or header[1:4] != b"\0\0\0":
            raise EngineError("malformed log stream")
        remaining = int.from_bytes(header[4:8], "big")
        consumed += LOG_HEADER_BYTES
        while remaining:
            chunk = response.read(min(remaining, LOG_READ_CHUNK))
            if not chunk:
                return
            remaining -= len(chunk)
            consumed += len(chunk)
            collector.feed(stream, chunk)


class _LineCollector:
    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        self._pending: dict[int, bytearray] = {}

    def feed(self, stream: int, data: bytes) -> None:
        pending = self._pending.setdefault(stream, bytearray())
        pieces = data.split(b"\n")
        for index, piece in enumerate(pieces):
            room = MAX_LOG_LINE_BYTES - len(pending)
            if room > 0:
                pending.extend(piece[:room])
            if index < len(pieces) - 1:
                self._push(pending)

    def lines(self) -> list[str]:
        for pending in self._pending.values():
            if pending:
                self._push(pending)
        return list(self._lines)

    def _push(self, pending: bytearray) -> None:
        text = bytes(pending).decode("utf-8", errors="replace").rstrip("\r")
        pending.clear()
        self._lines.append("".join(c if c == "\t" or c >= " " else " " for c in text))
