"""Whole-stack logs: the merged stream and download of a Compose or Swarm stack, and its tab."""

import asyncio
from collections.abc import AsyncGenerator, Collection, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal, Protocol
from urllib.parse import urlencode

import anyio
from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.auth.dependencies import CurrentUser, require_user
from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.assembly import app_stack_services, app_swarm_services
from app.services.container_logs import (
    DEFAULT_PAGE_TAIL,
    MAX_FILTER_CHARS,
    MAX_PAGE_TAIL,
    RETRY_MILLISECONDS,
    LogQuery,
)
from app.services.container_stats import KeepAlive
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.object_locks import TooManyStreams
from app.services.stack_document import STACK_NAME
from app.services.stack_logs import (
    MAX_MEMBERS,
    StackLogDownload,
    StackLogItem,
    StackLogMembers,
    StackLogNotice,
    StackLogService,
    StackLogStream,
    StackLogTail,
)
from app.web.container_routes import (
    CHECKED_FLAG,
    LOG_BUDGET_CHOICES,
    LOG_FALSE,
    LOG_PARAMS,
    LOG_SINCE_PRESETS,
    LOG_TAIL_CHOICES,
    LOG_TAIL_DIGITS,
    RECENT_PARAM,
    _log_time,
)
from app.web.container_stream_routes import (
    DOWNLOAD_HEADERS,
    SSE_MEDIA_TYPE,
    STREAM_HEADERS,
    _event,
    _invalid_query,
    _refusal,
)
from app.web.identifiers import PathId
from app.web.object_pages import self_href
from app.web.swarm_routes import Namespace
from app.web.templating import is_htmx_request

__all__ = ["LOGS_TAB", "logs_status", "logs_tab_context", "router"]

LOGS_TAB: Final = "logs"
SERVICES_PARAM: Final = "services"
SERVICE_VALUES_MAX: Final = 256
SERVICE_NAME_MAX: Final = 255
SEGMENT_MAX: Final = 128
UNAVAILABLE: Final = (EnvironmentUnavailable, ObjectTooLarge)

type StackKind = Literal["compose", "swarm"]

router = APIRouter(
    prefix="/environments/{environment_id}",
    tags=["stacks"],
    dependencies=[Depends(require_user)],
)


def _stack_name(stack_name: Annotated[str, Path(max_length=SEGMENT_MAX)]) -> str:
    if STACK_NAME.fullmatch(stack_name) is None:
        raise ResourceNotFound("not found")
    return stack_name


StackName = Annotated[str, Depends(_stack_name)]


@dataclass(frozen=True, slots=True)
class _Routes:
    stream: str
    download: str
    parameter: str


ROUTES: Final[Mapping[StackKind, _Routes]] = {
    "compose": _Routes(
        "environment_stack_logs_stream", "environment_stack_logs_download", "stack_name"
    ),
    "swarm": _Routes(
        "environment_swarm_stack_logs_stream",
        "environment_swarm_stack_logs_download",
        "namespace",
    ),
}


class _Closable(Protocol):
    async def aclose(self) -> None: ...


def requested_services(request: Request) -> tuple[str, ...]:
    """Return the `services=` values of the query, deduplicated, bounded in count and length."""
    values = request.query_params.getlist(SERVICES_PARAM)[:SERVICE_VALUES_MAX]
    return tuple(dict.fromkeys(v for v in values if v and len(v) <= SERVICE_NAME_MAX))


def _frame(item: StackLogItem) -> str:
    if isinstance(item, KeepAlive):
        return ": keep-alive\n\n"
    if isinstance(item, StackLogNotice):
        body = "".join(f"data: {line}\n" for line in item.text.split("\n"))
        return f"event: notice\n{body}\n"
    return _event(item)


async def _next_item(stream: StackLogStream) -> StackLogItem | None:
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return None


async def _log_frames(stream: StackLogStream) -> AsyncGenerator[str, None]:
    """Render each batch and notice as an event and a quiet interval as a comment line.

    Each wait runs as its own task so that a cancelled response never cancels the stream in the
    middle of releasing its Engine responses; the release runs shielded, after that task ends.
    """
    pending: asyncio.Future[StackLogItem | None] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(_next_item(stream))
            item = await asyncio.shield(pending)
            pending = None
            if item is None:
                break
            yield _frame(item)
        match stream.end_reason:
            case "lifetime":
                yield f"retry: {RETRY_MILLISECONDS}\n\n"
            case "finished" | "unavailable" as reason:
                yield f"event: end\ndata: {reason}\n\n"
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()


async def _download_chunks(download: StackLogDownload) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in download:
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await download.aclose()


class _ClosingResponse(StreamingResponse):
    """A streamed response that closes its source on every way out.

    The generator's own cleanup never runs if the client leaves before the first chunk is
    requested, so the release is tied to the response instead.
    """

    def __init__(
        self,
        source: _Closable,
        content: AsyncGenerator[str, None] | AsyncGenerator[bytes, None],
        *,
        media_type: str,
        headers: Mapping[str, str],
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._source = source
        self._content = content

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._content.aclose()
                await self._source.aclose()


async def _open_stream(
    request: Request, actor: Principal, environment_id: int, name: str, kind: StackKind
) -> StackLogStream:
    query = LogQuery.from_params(request.query_params)
    services = requested_services(request)
    last_event_id = request.headers.get("last-event-id")
    after = request.query_params.get("after")
    if kind == "compose":
        async with app_stack_services(request.app) as stacks:
            return await stacks.logs.compose_open(
                actor, environment_id, name, query, services, last_event_id, after
            )
    async with app_swarm_services(request.app) as swarm:
        return await swarm.stack_logs.swarm_open(
            actor, environment_id, name, query, services, last_event_id, after
        )


async def _open_download(
    request: Request, actor: Principal, environment_id: int, name: str, kind: StackKind
) -> StackLogDownload:
    query = LogQuery.from_params(request.query_params, allow_all=True)
    services = requested_services(request)
    if kind == "compose":
        async with app_stack_services(request.app) as stacks:
            return await stacks.logs.compose_download(actor, environment_id, name, query, services)
    async with app_swarm_services(request.app) as swarm:
        return await swarm.stack_logs.swarm_download(actor, environment_id, name, query, services)


async def _stream_response(
    request: Request, actor: Principal, environment_id: int, name: str, kind: StackKind
) -> Response:
    try:
        stream = await _open_stream(request, actor, environment_id, name, kind)
    except ValidationFailed as invalid:
        return _invalid_query(invalid)
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except UNAVAILABLE as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _ClosingResponse(
            stream, _log_frames(stream), media_type=SSE_MEDIA_TYPE, headers=STREAM_HEADERS
        )
    except BaseException:
        await stream.aclose()
        raise


async def _download_response(
    request: Request, actor: Principal, environment_id: int, name: str, kind: StackKind
) -> Response:
    try:
        download = await _open_download(request, actor, environment_id, name, kind)
    except ValidationFailed as invalid:
        return _invalid_query(invalid)
    except TooManyStreams as refused:
        return _refusal(request, "Too many live views are open", str(refused), 429)
    except UNAVAILABLE as unavailable:
        return _refusal(request, "Logs are not available", str(unavailable), 503)
    try:
        return _ClosingResponse(
            download,
            _download_chunks(download),
            media_type=download.media_type,
            headers={
                **DOWNLOAD_HEADERS,
                "Content-Disposition": f'attachment; filename="{download.filename}"',
            },
        )
    except BaseException:
        await download.aclose()
        raise


@router.get(
    "/stacks/{stack_name}/logs/stream",
    response_class=StreamingResponse,
    name="environment_stack_logs_stream",
)
async def compose_logs_stream(
    stack_name: StackName, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Stream the stack's merged logs as server-sent events; read access.

    Events `out` and `err` carry up to 200 lines of `<timestamp> <member> <text>`, `notice` a
    line of the view's own, `end` says `finished` or `unavailable`. Repeated `services` values
    limit the members. The `Last-Event-ID` header, or `after` in nanoseconds, resumes every
    member after it.
    """
    return await _stream_response(request, actor, environment_id, stack_name, "compose")


@router.get(
    "/stacks/{stack_name}/logs/download",
    response_class=StreamingResponse,
    name="environment_stack_logs_download",
)
async def compose_logs_download(
    stack_name: StackName, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Download a capped rendering of the stack's merged logs; read access.

    The `format` query value is `text` (the default) or `ndjson`; repeated `services` values
    limit the members.
    """
    return await _download_response(request, actor, environment_id, stack_name, "compose")


@router.get(
    "/swarm/stacks/{namespace}/logs/stream",
    response_class=StreamingResponse,
    name="environment_swarm_stack_logs_stream",
)
async def swarm_logs_stream(
    namespace: Namespace, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Stream the stack's merged logs as server-sent events; read access.

    Events `out` and `err` carry up to 200 lines of `<timestamp> <member> <text>`, `notice` a
    line of the view's own, `end` says `finished` or `unavailable`. Repeated `services` values
    limit the members. The `Last-Event-ID` header, or `after` in nanoseconds, resumes every
    member after it.
    """
    return await _stream_response(request, actor, environment_id, namespace, "swarm")


@router.get(
    "/swarm/stacks/{namespace}/logs/download",
    response_class=StreamingResponse,
    name="environment_swarm_stack_logs_download",
)
async def swarm_logs_download(
    namespace: Namespace, request: Request, actor: CurrentUser, environment_id: PathId
) -> Response:
    """Download a capped rendering of the stack's merged logs; read access.

    The `format` query value is `text` (the default) or `ndjson`; repeated `services` values
    limit the members.
    """
    return await _download_response(request, actor, environment_id, namespace, "swarm")


def _checked(present: Mapping[str, str], name: str) -> bool:
    return present.get(name, "1").strip().lower() not in LOG_FALSE


def _failure(status: int, title: str, error: Exception) -> dict[str, Any]:
    return {"status": status, "title": title, "message": str(error)}


def _page_query(present: Mapping[str, str]) -> tuple[LogQuery | None, dict[str, str], bool]:
    params = dict(present)
    clamped = False
    requested = params.get("tail", "").strip()
    if (
        requested.isascii()
        and requested.isdigit()
        and len(requested) <= LOG_TAIL_DIGITS
        and int(requested) > MAX_PAGE_TAIL
    ):
        params["tail"] = str(MAX_PAGE_TAIL)
        clamped = True
    try:
        query = LogQuery.from_params(params, default_tail=DEFAULT_PAGE_TAIL, max_tail=MAX_PAGE_TAIL)
    except ValidationFailed as invalid:
        return None, dict(invalid.fields), clamped
    return query, {}, clamped


async def _read_view(
    logs: StackLogService,
    kind: StackKind,
    actor: Principal,
    environment_id: int,
    name: str,
    query: LogQuery | None,
    services: Collection[str],
) -> tuple[StackLogMembers | None, StackLogTail | None, dict[str, Any] | None]:
    """Read the tail when a query is given, else the members alone; failures become a notice."""
    tail_of = logs.compose_tail if kind == "compose" else logs.swarm_tail
    members_of = logs.compose_members if kind == "compose" else logs.swarm_members
    failure: dict[str, Any] | None = None
    if query is not None:
        try:
            tail = await tail_of(actor, environment_id, name, query, services)
        except TooManyStreams as refused:
            failure = _failure(429, "Too many live views are open", refused)
        except UNAVAILABLE as unavailable:
            failure = _failure(503, "Logs are not available", unavailable)
        else:
            return tail.members, tail, None
    try:
        info = await members_of(actor, environment_id, name, services)
    except UNAVAILABLE as unavailable:
        return None, None, failure or _failure(503, "Logs are not available", unavailable)
    return info, None, failure


async def logs_tab_context(
    request: Request,
    logs: StackLogService,
    kind: StackKind,
    actor: Principal,
    environment_id: int,
    name: str,
    detail_href: str,
) -> dict[str, Any]:
    """Read the Logs tab's members and form values, and on request the tail shown without script.

    Script streams the output itself, so only a plain load carrying `recent=1` reads the tail.
    """
    present = {key: request.query_params[key] for key in LOG_PARAMS if key in request.query_params}
    query, errors, clamped = _page_query(present)
    services = requested_services(request)
    recent = (
        query is not None
        and request.query_params.get(RECENT_PARAM) == CHECKED_FLAG
        and not is_htmx_request(request)
    )
    info, tail, failure = await _read_view(
        logs, kind, actor, environment_id, name, query if recent else None, services
    )
    lines: list[dict[str, Any]] = []
    if tail is not None and query is not None:
        folded = query.filter.casefold()
        lines = [
            {
                "time": _log_time(line.timestamp),
                "origin": line.origin,
                "service": line.service,
                "text": line.text,
                "stderr": line.stream == "stderr",
            }
            for line in tail.lines
            if not folded or folded in line.text.casefold()
        ]
    choices = info.services if info is not None else ()
    selected = info.selected if info is not None else ()
    filtered = frozenset(selected) != frozenset(choices)
    carried = [(SERVICES_PARAM, service) for service in selected] if filtered else []
    tail_value = (present.get("tail", "").strip() or str(DEFAULT_PAGE_TAIL))[:16]
    tail_options = [str(choice) for choice in LOG_TAIL_CHOICES]
    if tail_value not in tail_options:
        tail_options.insert(0, tail_value)
    values = {
        "tail": tail_value,
        "since": present.get("since", ""),
        "until": present.get("until", ""),
        "stdout": _checked(present, "stdout"),
        "stderr": _checked(present, "stderr"),
        "timestamps": _checked(present, "timestamps"),
        "q": present.get("q", ""),
    }
    flags = [
        ("stdout", "1" if values["stdout"] else "0"),
        ("stderr", "1" if values["stderr"] else "0"),
        ("timestamps", "1" if values["timestamps"] else "0"),
    ]
    routes = ROUTES[kind]
    path_parameters: dict[str, Any] = {"environment_id": environment_id, routes.parameter: name}
    download_base = str(request.app.url_path_for(routes.download, **path_parameters))
    download_params = [("tail", "all")]
    download_params.extend((key, present[key]) for key in ("since", "until") if present.get(key))
    download_params.extend(flags)
    if present.get("q"):
        download_params.append(("q", present["q"]))
    download_params.extend(carried)
    download_href = f"{download_base}?{urlencode(download_params)}"
    recent_params = [("tab", LOGS_TAB), ("tail", tail_value)]
    recent_params.extend((key, present[key]) for key in ("since", "until", "q") if present.get(key))
    recent_params.extend(flags)
    recent_params.extend(carried)
    recent_params.append((RECENT_PARAM, CHECKED_FLAG))
    return {
        "kind": kind,
        "name": name,
        "services": choices,
        "selected": selected,
        "filtered": filtered,
        "choices": [{"name": service, "checked": service in selected} for service in choices],
        "members": info.members if info is not None else 0,
        "hidden": info.hidden if info is not None else 0,
        "over_cap": info.over_cap if info is not None else 0,
        "unreadable": (
            tail.unreadable if tail is not None else info.unreadable if info is not None else 0
        ),
        "max_members": MAX_MEMBERS,
        "selection": values,
        "errors": errors,
        "tail_options": tail_options,
        "budget_options": LOG_BUDGET_CHOICES,
        "since_presets": LOG_SINCE_PRESETS,
        "max_filter": MAX_FILTER_CHARS,
        "static": query is not None,
        "recent": recent,
        "lines": lines,
        "total": len(tail.lines) if tail is not None else 0,
        "cut": tail.cut if tail is not None else False,
        "clamped": clamped,
        "failure": failure,
        "page_tail": DEFAULT_PAGE_TAIL if query is None else (query.tail or 0),
        "running": True,
        "stream_href": str(request.app.url_path_for(routes.stream, **path_parameters)),
        "services_query": urlencode(carried),
        "download_base": download_base,
        "download_href": download_href,
        "download_text_href": f"{download_href}&format=text",
        "download_ndjson_href": f"{download_href}&format=ndjson",
        "refresh_href": self_href(request),
        "recent_href": f"{detail_href}?{urlencode(recent_params)}",
        "action_href": detail_href,
    }


def logs_status(logs: Mapping[str, Any], status_code: int) -> int:
    """Return the page status the Logs tab calls for: 400 for a bad form, the failure's own."""
    if logs["errors"]:
        return 400
    failure = logs["failure"]
    return int(failure["status"]) if failure is not None else status_code
