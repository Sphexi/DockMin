"""Streams a multipart archive upload to its consumer after checking the CSRF token."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException, Request
from python_multipart.exceptions import FormParserError, MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from app.web.csrf import CSRF_FORM_FIELD, verify_presented_token

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

ARCHIVE_FIELD: Final = "archive"
MAX_TOKEN_BYTES: Final = 128
MAX_BYTES_OUTSIDE_FILE: Final = 64 * 1024
MAX_FIELD_BYTES: Final = 256
SLICE_BYTES: Final = 16 * 1024
UPLOAD_IDLE_SECONDS: Final = 120.0
MULTIPART: Final = b"multipart/form-data"
_FORM_DATA: Final = b"form-data"

type _Event = tuple[str, bytes | dict[bytes, bytes]]


def _bad_request() -> HTTPException:
    return HTTPException(status_code=400)


class _Events:
    """Collects what the push parser reports so an async generator can pull it."""

    def __init__(self) -> None:
        self.pending: list[_Event] = []
        self._headers: dict[bytes, bytes] = {}
        self._name = bytearray()
        self._value = bytearray()

    def callbacks(self) -> "MultipartCallbacks":
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def take(self) -> list[_Event]:
        taken, self.pending = self.pending, []
        return taken

    def _part_begin(self) -> None:
        self._headers = {}

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        self._headers[bytes(self._name).lower()] = bytes(self._value)
        self._name.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        self.pending.append(("part", self._headers))

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        self.pending.append(("data", data[start:end]))

    def _part_end(self) -> None:
        self.pending.append(("end", b""))


def _boundary(request: Request) -> bytes:
    kind, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if kind != MULTIPART or not boundary:
        raise _bad_request()
    return boundary


def _disposition(headers: dict[bytes, bytes]) -> dict[bytes, bytes]:
    kind, options = parse_options_header(headers.get(b"content-disposition", b""))
    if kind != _FORM_DATA:
        raise _bad_request()
    return options


async def _bounded_stream(request: Request, idle_seconds: float) -> AsyncIterator[bytes]:
    """Yield the request body, answering 408 when no chunk arrives within idle_seconds."""
    chunks = request.stream().__aiter__()
    while True:
        try:
            async with asyncio.timeout(idle_seconds):
                chunk = await anext(chunks)
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise HTTPException(
                status_code=HTTPStatus.REQUEST_TIMEOUT, headers={"Connection": "close"}
            ) from None
        yield chunk


async def _file_chunks(
    request: Request,
    idle_seconds: float,
    fields: dict[str, str] | None = None,
    field_names: frozenset[str] = frozenset(),
) -> AsyncIterator[bytes]:
    events = _Events()
    try:
        parser = MultipartParser(_boundary(request), events.callbacks())
    except FormParserError:
        raise _bad_request() from None
    stage = "token_part"
    token = bytearray()
    field = bytearray()
    field_name = ""
    outside = 0
    async for chunk in _bounded_stream(request, idle_seconds):
        for offset in range(0, len(chunk), SLICE_BYTES):
            piece = chunk[offset : offset + SLICE_BYTES]
            if stage != "file_data":
                outside += len(piece)
                if outside > MAX_BYTES_OUTSIDE_FILE:
                    raise _bad_request()
            try:
                parser.write(piece)
            except (MultipartParseError, FormParserError):
                raise _bad_request() from None
            for kind, payload in events.take():
                if kind == "part":
                    assert isinstance(payload, dict)
                    options = _disposition(payload)
                    name = options.get(b"name")
                    has_file = b"filename" in options
                    if stage == "token_part" and name == CSRF_FORM_FIELD.encode() and not has_file:
                        stage = "token_data"
                    elif stage == "file_part" and name == ARCHIVE_FIELD.encode() and has_file:
                        stage = "file_data"
                    elif stage == "file_part" and _is_new_field(
                        name, has_file, fields, field_names
                    ):
                        field_name = name.decode("ascii") if name else ""
                        field.clear()
                        stage = "field_data"
                    else:
                        raise _bad_request()
                elif kind == "data":
                    assert isinstance(payload, bytes)
                    if stage == "token_data":
                        token += payload
                        if len(token) > MAX_TOKEN_BYTES:
                            raise _bad_request()
                    elif stage == "field_data":
                        field += payload
                        if len(field) > MAX_FIELD_BYTES:
                            raise _bad_request()
                    elif stage == "file_data":
                        yield payload
                    else:
                        raise _bad_request()
                elif stage == "token_data":
                    verify_presented_token(request, _decode_token(token))
                    stage = "file_part"
                elif stage == "field_data":
                    assert fields is not None
                    try:
                        fields[field_name] = field.decode("utf-8")
                    except UnicodeDecodeError:
                        raise _bad_request() from None
                    stage = "file_part"
                elif stage == "file_data":
                    stage = "done"
                else:
                    raise _bad_request()
    try:
        parser.finalize()
    except (MultipartParseError, FormParserError):
        raise _bad_request() from None
    if stage != "done":
        raise _bad_request()


def _is_new_field(
    name: bytes | None, has_file: bool, fields: dict[str, str] | None, field_names: frozenset[str]
) -> bool:
    if name is None or has_file or fields is None:
        return False
    text = name.decode("ascii", "replace")
    return text in field_names and text not in fields


def _decode_token(token: bytearray) -> str | None:
    try:
        return token.decode("ascii")
    except UnicodeDecodeError:
        return None


async def _resume(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first
    async for chunk in rest:
        yield chunk


async def read_archive_upload[T](
    request: Request,
    sink: Callable[[AsyncIterator[bytes]], Awaitable[T]],
    *,
    idle_seconds: float = UPLOAD_IDLE_SECONDS,
    fields: dict[str, str] | None = None,
    field_names: frozenset[str] = frozenset(),
) -> T:
    """Hand the file part of a multipart upload to the sink as a stream and return its result.

    The first part must be the CSRF token. It is verified, with the Origin rule, and the first
    bytes of the file are read before the sink is called, so a request that fails either leaves
    the sink untouched. A last part holds the file. Anything else is answered 400, and a body
    that stalls for idle_seconds is answered 408. Each of `field_names` may appear once between
    the token and the file as a text part of at most MAX_FIELD_BYTES; `fields` receives them,
    all of them before the sink runs.
    """
    chunks = _file_chunks(request, idle_seconds, fields, field_names)
    first = await anext(chunks, None)
    return await sink(chunks if first is None else _resume(first, chunks))
