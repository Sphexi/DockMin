"""The Engine calls this product makes, grouped by resource, as thin typed methods."""

import asyncio
import base64
import binascii
import json
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.docker_engine.archive import (
    ArchiveEntry,
    DirectoryStream,
    open_directory,
    read_archive,
    write_archive,
)
from app.docker_engine.engine_http import (
    ACTION_REPLY_BYTES,
    COUNT_RESPONSE_BYTES,
    EngineHttp,
    Params,
)
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, RequestOutcomeUnknown
from app.docker_engine.frames import (
    FrameDecodeError,
    LineDecoder,
    LogFrame,
    LogFrameDecoder,
    TruncatedStream,
)
from app.docker_engine.labels import (
    budget_inspect,
    budget_networks,
    budget_service,
    budget_services,
    budget_spec_object,
    budget_spec_objects,
    budget_summaries,
    budget_volumes,
)
from app.docker_engine.models import (
    ConfigDocument,
    ContainerInspect,
    ContainerStats,
    ContainerSummary,
    DistributionDescriptor,
    EngineEvent,
    ExecInspect,
    ImageDeleteItem,
    ImageDiskUsage,
    ImageHistoryEntry,
    ImageInspect,
    ImageSummary,
    Network,
    NodeDocument,
    ObjectVersion,
    PullProgress,
    PushProgress,
    ServiceDocument,
    SwarmCluster,
    SwarmConfig,
    SwarmNode,
    SwarmSecret,
    SwarmService,
    SwarmTask,
    SystemInfo,
    Volume,
)
from app.docker_engine.streams import BackoffPolicy, decoded_stream, watch_events
from app.docker_engine.timeouts import Timeouts
from app.docker_engine.transport import HijackedConnection
from app.logging import get_logger

logger = get_logger(__name__)

type Filters = Mapping[str, list[str]]

_NOT_MODIFIED = frozenset({304})
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_NETWORK_ID = re.compile(r"[0-9a-f]{64}|[0-9a-z]{25}")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_VOLUME_DRIVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_REGISTRY_AUTH = re.compile(r"[A-Za-z0-9_=+/-]+")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_DEFAULT_PULL_TAG = "latest"
ANONYMOUS_REGISTRY_AUTH = "e30="
_DETACH_KEYS = re.compile(r"[A-Za-z0-9@^_,\[\]-]{1,64}")
_LOG_TIME_TEXT = re.compile(r"[0-9]{1,19}(\.[0-9]{1,9})?")
_NANOS_PER_SECOND = 1_000_000_000
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_SWARM_NAMESPACE_LABEL = "com.docker.stack.namespace"
_SWARM_ID = re.compile(r"[0-9a-z]{25}")
_SERVICE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?")
_SWARM_OBJECT_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?")
_RAW_STREAM = "application/vnd.docker.raw-stream"
_MULTIPLEXED_STREAM = "application/vnd.docker.multiplexed-stream"
# Keys a service create or update body may carry.
_SERVICE_SPEC_KEYS = frozenset(
    {"Name", "Labels", "TaskTemplate", "Mode", "UpdateConfig", "RollbackConfig", "EndpointSpec"}
)
_NODE_SPEC_KEYS = frozenset({"Name", "Labels", "Role", "Availability"})
_REGISTRY_AUTH_SOURCES = frozenset({"spec", "previous-spec"})
_CONFIG_DATA_MAX_BYTES = 1_023_999
_SECRET_DATA_MAX_BYTES = 511_999
# Keys a network create or connect body may carry.
_NETWORK_CREATE_KEYS = frozenset(
    {"Name", "Driver", "Internal", "Attachable", "EnableIPv6", "IPAM", "Options", "Labels"}
)
_CONNECT_KEYS = frozenset({"Container", "EndpointConfig"})
_ENDPOINT_CONFIG_KEYS = frozenset({"Aliases", "IPAMConfig"})
_ENDPOINT_IPAM_KEYS = frozenset({"IPv4Address", "IPv6Address"})
# Everything an exec body may carry; Privileged and Env are deliberately absent.
_EXEC_BODY_KEYS = frozenset(
    {
        "Cmd",
        "ConsoleSize",
        "WorkingDir",
        "User",
        "DetachKeys",
        "Tty",
        "AttachStdin",
        "AttachStdout",
        "AttachStderr",
    }
)


class _LabelledContainer(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    labels: dict[str, str | None] | None = Field(default=None, alias="Labels")


class _LabelledSpec(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    labels: dict[str, str | None] | None = Field(default=None, alias="Labels")


class _LabelledService(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    spec: _LabelledSpec = Field(default_factory=_LabelledSpec, alias="Spec")


class _SwarmCreateReply(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    id: str = Field(alias="ID")
    warnings: list[str] | None = Field(default=None, alias="Warnings")


class _CreateReply(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    id: str = Field(alias="Id")
    warnings: list[str] | None = Field(default=None, alias="Warnings")


INFO_RESPONSE_BYTES = COUNT_RESPONSE_BYTES
CONTAINER_LIST_BYTES = 32 * 1024 * 1024
CONTAINER_IDS_BYTES = 4 * 1024 * 1024
CONTAINER_INSPECT_BYTES = 4 * 1024 * 1024
VOLUME_INSPECT_BYTES = 4 * 1024 * 1024
IMAGE_INSPECT_BYTES = 4 * 1024 * 1024
IMAGE_LIST_BYTES = 16 * 1024 * 1024
DISK_USAGE_BYTES = 16 * 1024 * 1024
IMAGE_HISTORY_BYTES = 1024 * 1024
IMAGE_REMOVE_REPLY_BYTES = 1024 * 1024
MAX_HISTORY_ROWS = 512
PROGRESS_LINE_BYTES = 16 * 1024
PROGRESS_THREADED_BYTES = 256 * 1024
_NONE_PLACEHOLDERS = frozenset({"<none>:<none>", "<none>@<none>"})
NETWORK_INSPECT_BYTES = 4 * 1024 * 1024
NETWORK_LIST_BYTES = 16 * 1024 * 1024
NETWORK_WRITE_REPLY_BYTES = 64 * 1024
VOLUME_LIST_BYTES = 16 * 1024 * 1024
VOLUME_CREATE_REPLY_BYTES = 1024 * 1024
CONTAINER_CREATE_REPLY_BYTES = 64 * 1024
STATS_SAMPLE_BYTES = 1024 * 1024
STATS_FRAME_BYTES = 256 * 1024
LOG_FRAME_BYTES = 1024 * 1024
EXEC_INSPECT_BYTES = 64 * 1024
SWARM_INSPECT_BYTES = 1024 * 1024
STACK_NAMESPACES_BYTES = 4 * 1024 * 1024
SERVICE_LIST_BYTES = 16 * 1024 * 1024
SERVICE_INSPECT_BYTES = 4 * 1024 * 1024
SWARM_WRITE_REPLY_BYTES = 64 * 1024
TASK_LIST_BYTES = 16 * 1024 * 1024
TASK_INSPECT_BYTES = 1024 * 1024
NODE_LIST_BYTES = 4 * 1024 * 1024
NODE_INSPECT_BYTES = 1024 * 1024
CONFIG_LIST_BYTES = 32 * 1024 * 1024
CONFIG_INSPECT_BYTES = 4 * 1024 * 1024
SECRET_LIST_BYTES = 16 * 1024 * 1024
SECRET_INSPECT_BYTES = 1024 * 1024
DISTRIBUTION_BYTES = 1024 * 1024
MAX_TERMINAL_ROWS = 500
MAX_TERMINAL_COLUMNS = 1000
DETACH_KEYS = "ctrl-@,ctrl-^,ctrl-@,ctrl-^"


def _encode_json(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def _filter_params(
    filters: Filters | None, **extra: str | int | bool
) -> dict[str, str | int | bool]:
    params: dict[str, str | int | bool] = dict(extra)
    if filters:
        params["filters"] = json.dumps(filters)
    return params


def _waiting(daemon_wait_seconds: int | None) -> Timeouts:
    if daemon_wait_seconds is None:
        return Timeouts.long()
    return Timeouts.long_for(max(daemon_wait_seconds, 0))


def format_engine_timestamp(nanoseconds: int) -> str:
    """Format Unix nanoseconds as the `seconds.nanoseconds` text the Engine's time filters read."""
    seconds, nanos = divmod(nanoseconds, _NANOS_PER_SECOND)
    return f"{seconds}.{nanos:09d}"


def _valid_terminal_size(rows: int, cols: int) -> bool:
    return (
        not isinstance(rows, bool)
        and not isinstance(cols, bool)
        and 1 <= rows <= MAX_TERMINAL_ROWS
        and 1 <= cols <= MAX_TERMINAL_COLUMNS
    )


def _terminal_size(http: EngineHttp, rows: int, cols: int) -> dict[str, int]:
    if not _valid_terminal_size(rows, cols):
        raise EngineError(http.environment_id, "terminal size is out of range")
    return {"h": rows, "w": cols}


def exec_create_body(
    command: Sequence[str],
    *,
    rows: int,
    cols: int,
    working_dir: str | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    """Build the variable part of an exec body; `create_exec` adds the fixed terminal options.

    The command is an argument list and is never joined into a string. ValueError for an empty
    command or a size outside the bounds `resize` accepts.
    """
    if not command:
        raise ValueError("exec command is empty")
    if not _valid_terminal_size(rows, cols):
        raise ValueError("terminal size is out of range")
    body: dict[str, Any] = {"Cmd": list(command), "ConsoleSize": [rows, cols]}
    if working_dir is not None:
        body["WorkingDir"] = working_dir
    if user is not None:
        body["User"] = user
    return body


def _registry_auth_headers(
    http: EngineHttp, registry_auth: SecretStr | None, *, always: bool
) -> dict[str, str] | None:
    if registry_auth is None:
        return {"X-Registry-Auth": ANONYMOUS_REGISTRY_AUTH} if always else None
    value = registry_auth.get_secret_value()
    if not _REGISTRY_AUTH.fullmatch(value):
        raise EngineError(http.environment_id, "registry credential is not base64")
    return {"X-Registry-Auth": value}


def _time_bound(http: EngineHttp, value: int | str) -> int | str:
    valid = (
        _LOG_TIME_TEXT.fullmatch(value) is not None
        if isinstance(value, str)
        else not isinstance(value, bool) and value >= 0
    )
    if not valid:
        raise EngineError(http.environment_id, "log time bound is malformed")
    return value


def _swarm_log_params(
    http: EngineHttp,
    *,
    follow: bool,
    stdout: bool,
    stderr: bool,
    since: int | str | None,
    until: int | str | None,
    tail: int | None,
) -> dict[str, str | int | bool]:
    params: dict[str, str | int | bool] = {
        "follow": follow,
        "stdout": stdout,
        "stderr": stderr,
        "timestamps": True,
        "details": True,
    }
    if since is not None:
        params["since"] = _time_bound(http, since)
    if until is not None:
        params["until"] = _time_bound(http, until)
    if tail is not None:
        if isinstance(tail, bool) or tail < 0:
            raise EngineError(http.environment_id, "log tail is out of range")
        params["tail"] = tail
    return params


def _created_reply(http: EngineHttp, response: httpx.Response, subject: str) -> _CreateReply:
    try:
        document = json.loads(response.content)
    except (ValueError, RecursionError):
        raise EngineError(http.environment_id, f"{subject} reply is not valid JSON") from None
    reply = http.parse(_CreateReply, document)
    if _CONTAINER_ID.fullmatch(reply.id) is None:
        raise EngineError(http.environment_id, f"{subject} reply carries a malformed id")
    return reply


async def _raw_stream(http: EngineHttp, connection: HijackedConnection) -> HijackedConnection:
    if connection.multiplexed:
        await connection.close()
        raise EngineError(http.environment_id, "the Engine offered a multiplexed stream")
    return connection


class ContainersApi:
    """Container lifecycle, inspection, logs and stats."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(
        self, *, all: bool = False, filters: Filters | None = None
    ) -> list[ContainerSummary]:
        """List containers; stopped ones only when `all` is set.

        The reply is capped at 32 MiB and decoded in a worker thread, with each container's
        labels cut to a fixed budget before validation. ResponseTooLarge past the cap; a partial
        list is never returned.
        """
        return await self._http.get_parsed_list(
            ContainerSummary,
            "/containers/json",
            params=_filter_params(filters, all=all),
            max_bytes=CONTAINER_LIST_BYTES,
            prepare=budget_summaries,
        )

    async def list_ids(
        self, *, all: bool = False, filters: Filters | None = None
    ) -> frozenset[str]:
        """Return only the ids of a listing; the reply is capped at 4 MiB (ResponseTooLarge)."""
        return await self._http.get_ids(
            "/containers/json",
            params=_filter_params(filters, all=all),
            max_bytes=CONTAINER_IDS_BYTES,
        )

    async def count(self, *, all: bool = False, filters: Filters | None = None) -> int:
        """Count containers from a size-limited reply; ResponseTooLarge past the limit."""
        return await self._http.count_items(
            "/containers/json", params=_filter_params(filters, all=all)
        )

    async def compose_projects(self) -> frozenset[str]:
        """Return the case-folded names of the Compose projects that own a container.

        Stopped containers count. The Engine filters on the label key alone, the reply is capped
        at 4 MiB and decoded in a worker thread with the label budget applied, and only the
        project label is read. ResponseTooLarge past the cap; a partial set is never returned.
        """
        containers = await self._http.get_parsed_list(
            _LabelledContainer,
            "/containers/json",
            params=_filter_params({"label": [_COMPOSE_PROJECT_LABEL]}, all=True),
            max_bytes=CONTAINER_IDS_BYTES,
            prepare=budget_summaries,
        )
        return frozenset(
            value.casefold()
            for container in containers
            for key, value in (container.labels or {}).items()
            if key == _COMPOSE_PROJECT_LABEL and value
        )

    async def inspect(self, container_id: str) -> ContainerInspect:
        """Fetch one container's detail; the reply is capped at 4 MiB (ResponseTooLarge)."""
        cid = self._http.segment(container_id)
        return await self._http.get_parsed(
            ContainerInspect,
            f"/containers/{cid}/json",
            max_bytes=CONTAINER_INSPECT_BYTES,
            prepare=budget_inspect,
        )

    async def inspect_document(self, container_id: str) -> dict[str, Any]:
        """Fetch one container's inspect document as the Engine sent it, capped at 4 MiB.

        Decoded in a worker thread. The Engine resolves names and id prefixes, so the caller
        checks the returned `Id`.
        """
        cid = self._http.segment(container_id)
        response = await self._http.request(
            "GET", f"/containers/{cid}/json", max_bytes=CONTAINER_INSPECT_BYTES
        )
        return await asyncio.to_thread(_json_object, self._http, response.content, "container")

    async def archive(self, container_id: str, path: str, *, limit: int) -> ArchiveEntry:
        """Read one regular file of a container, created or running, at an absolute path.

        Refused with an `ArchiveError` subclass unless the path is a regular file of at most
        `limit` bytes, found as exactly one tar member named after it. `container_id` must be a
        full 64-hex id.
        """
        return await read_archive(self._http, container_id, path, limit=limit)

    async def archive_directory(self, container_id: str, path: str) -> DirectoryStream:
        """Stream the tar of one directory of a container, created or running.

        The stat header is checked as a directory and the first chunk is read before this
        returns; the body is never held whole. No size cap and no total time limit; a read that
        receives nothing for 60 seconds fails. The caller must close the stream. `container_id`
        must be a full 64-hex id.
        """
        return await open_directory(self._http, container_id, path)

    async def put_archive(self, container_id: str, path: str, chunks: AsyncIterator[bytes]) -> None:
        """Extract a tar, sent as it is produced, into a directory of a container.

        An existing directory is never replaced by a non-directory or the reverse. Fails with
        an `ArchiveError` subclass when the Engine refuses the archive; a write that sends
        nothing for 60 seconds fails. `container_id` must be a full 64-hex id.
        """
        await write_archive(self._http, container_id, path, chunks)

    async def create(self, body: Mapping[str, Any], *, name: str | None = None) -> str:
        """Create a container from an Engine create body and return its id.

        The body is encoded in a worker thread. The reply is capped at 64 KiB
        (ResponseTooLarge). EngineError when the returned id is not 64 lower-case hex digits. The
        Engine's warnings are counted in a debug log line and dropped.
        """
        params: Params = {} if name is None else {"name": self._http.segment(name)}
        encoded = await asyncio.to_thread(_encode_json, body)
        response = await self._http.request(
            "POST",
            "/containers/create",
            params=params,
            content=encoded,
            headers={"Content-Type": "application/json"},
            max_bytes=CONTAINER_CREATE_REPLY_BYTES,
        )
        reply = _created_reply(self._http, response, "container create")
        if reply.warnings:
            logger.debug(
                "container_create_warnings",
                extra={"environment_id": self._http.environment_id, "count": len(reply.warnings)},
            )
        return reply.id

    async def rename(self, container_id: str, new_name: str) -> None:
        """Give a container a new name."""
        cid = self._http.segment(container_id)
        await self._http.request(
            "POST",
            f"/containers/{cid}/rename",
            params={"name": self._http.segment(new_name)},
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def start(self, container_id: str) -> bool:
        """Start a container; False when it was already running, which is not an error."""
        cid = self._http.segment(container_id)
        response = await self._http.request(
            "POST",
            f"/containers/{cid}/start",
            also_ok=_NOT_MODIFIED,
            max_bytes=ACTION_REPLY_BYTES,
        )
        return response.status_code not in _NOT_MODIFIED

    async def stop(
        self,
        container_id: str,
        *,
        grace_seconds: int | None = None,
        daemon_wait_seconds: int | None = None,
    ) -> bool:
        """Stop a container; False when it was already stopped, which is not an error.

        `daemon_wait_seconds` is how long the daemon may wait before killing it; the read limit
        is extended to cover that.
        """
        cid = self._http.segment(container_id)
        params: Params = {} if grace_seconds is None else {"t": grace_seconds}
        response = await self._http.request(
            "POST",
            f"/containers/{cid}/stop",
            params=params,
            timeouts=_waiting(daemon_wait_seconds),
            also_ok=_NOT_MODIFIED,
            max_bytes=ACTION_REPLY_BYTES,
        )
        return response.status_code not in _NOT_MODIFIED

    async def restart(
        self,
        container_id: str,
        *,
        grace_seconds: int | None = None,
        daemon_wait_seconds: int | None = None,
    ) -> None:
        """Restart a container; `daemon_wait_seconds` extends the read limit as for `stop`."""
        cid = self._http.segment(container_id)
        params: Params = {} if grace_seconds is None else {"t": grace_seconds}
        await self._http.request(
            "POST",
            f"/containers/{cid}/restart",
            params=params,
            timeouts=_waiting(daemon_wait_seconds),
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def kill(self, container_id: str, *, signal: str | None = None) -> None:
        """Send a signal to a container, SIGKILL by default."""
        cid = self._http.segment(container_id)
        params: Params = {} if signal is None else {"signal": signal}
        await self._http.request(
            "POST", f"/containers/{cid}/kill", params=params, max_bytes=ACTION_REPLY_BYTES
        )

    async def pause(self, container_id: str) -> None:
        """Freeze every process in a running container."""
        cid = self._http.segment(container_id)
        await self._http.request("POST", f"/containers/{cid}/pause", max_bytes=ACTION_REPLY_BYTES)

    async def unpause(self, container_id: str) -> None:
        """Resume a paused container."""
        cid = self._http.segment(container_id)
        await self._http.request("POST", f"/containers/{cid}/unpause", max_bytes=ACTION_REPLY_BYTES)

    async def remove(
        self, container_id: str, *, force: bool = False, volumes: bool = False
    ) -> None:
        """Remove a container, optionally with its anonymous volumes."""
        cid = self._http.segment(container_id)
        await self._http.request(
            "DELETE",
            f"/containers/{cid}",
            params={"force": force, "v": volumes},
            timeouts=Timeouts.long(),
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def create_exec(self, container_id: str, body: Mapping[str, Any]) -> str:
        """Create an interactive TTY exec instance and return its id.

        `body` may carry only the keys of `exec_create_body`; any other key (`Privileged` and
        `Env` among them) is refused unsent. The terminal options and the detach keys are set
        here, whatever the body held. The reply is capped at 64 KiB (ResponseTooLarge).
        EngineError when the returned id is not 64 lower-case hex digits.
        """
        cid = self._http.segment(container_id)
        environment_id = self._http.environment_id
        if not body.keys() <= _EXEC_BODY_KEYS:
            raise EngineError(environment_id, "exec body carries an option that is not sent")
        command = body.get("Cmd")
        if (
            not isinstance(command, list | tuple)
            or not command
            or not all(isinstance(argument, str) for argument in command)
        ):
            raise EngineError(environment_id, "exec command must be a non-empty argument list")
        document = {
            **body,
            "Cmd": list(command),
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Tty": True,
            "DetachKeys": DETACH_KEYS,
        }
        response = await self._http.request(
            "POST",
            f"/containers/{cid}/exec",
            content=_encode_json(document),
            headers={"Content-Type": "application/json"},
            max_bytes=CONTAINER_CREATE_REPLY_BYTES,
        )
        return _created_reply(self._http, response, "exec create").id

    async def attach(self, container_id: str, *, detach_keys: str) -> HijackedConnection:
        """Attach to the container's main process over a hijacked, raw (TTY) stream.

        Sends stdin and receives stdout and stderr, without replaying logs. The caller owns the
        connection and must close it; closing detaches and leaves the container running.
        EngineError when the Engine answers with a multiplexed stream, which only a container
        without a TTY produces.
        """
        cid = self._http.segment(container_id)
        if _DETACH_KEYS.fullmatch(detach_keys) is None:
            raise EngineError(self._http.environment_id, "detach keys are malformed")
        connection = await self._http.hijack(
            f"/containers/{cid}/attach",
            params={
                "stream": 1,
                "stdin": 1,
                "stdout": 1,
                "stderr": 1,
                "logs": 0,
                "detachKeys": detach_keys,
            },
        )
        return await _raw_stream(self._http, connection)

    async def resize(self, container_id: str, rows: int, cols: int) -> None:
        """Resize the terminal of the container's main process.

        EngineError when rows are outside 1-500 or columns outside 1-1000.
        """
        cid = self._http.segment(container_id)
        await self._http.request(
            "POST",
            f"/containers/{cid}/resize",
            params=_terminal_size(self._http, rows, cols),
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def logs(
        self,
        container_id: str,
        *,
        tty: bool,
        follow: bool = True,
        stdout: bool = True,
        stderr: bool = True,
        since: int | str | None = None,
        until: int | str | None = None,
        tail: int | None = None,
        timestamps: bool = False,
        max_frame_bytes: int = LOG_FRAME_BYTES,
    ) -> AsyncGenerator[LogFrame, None]:
        """Stream container output as frames; `tty` must match the container's Tty setting.

        A TTY container's output is a raw byte stream with no frame headers, so the caller has
        to say which form to expect. `since` and `until` are Unix seconds or `seconds.nanos` text
        (see `format_engine_timestamp`). No frame is larger than `max_frame_bytes`: a multiplexed
        frame declaring more ends the stream with EngineError, and a raw chunk is split. Wrap in
        `contextlib.aclosing` to release the stream early.
        """
        cid = self._http.segment(container_id)
        if max_frame_bytes < 1:
            raise EngineError(self._http.environment_id, "log frame limit is not positive")
        params: dict[str, str | int | bool] = {
            "follow": follow,
            "stdout": stdout,
            "stderr": stderr,
            "timestamps": timestamps,
        }
        if since is not None:
            params["since"] = _time_bound(self._http, since)
        if until is not None:
            params["until"] = _time_bound(self._http, until)
        if tail is not None:
            params["tail"] = tail
        path = f"/containers/{cid}/logs"
        if tty:
            async with aclosing(self._http.stream_bytes("GET", path, params=params)) as chunks:
                async for chunk in chunks:
                    for start in range(0, len(chunk), max_frame_bytes):
                        yield LogFrame("stdout", chunk[start : start + max_frame_bytes])
            return
        async with aclosing(
            decoded_stream(self._http, LogFrameDecoder(max_frame_bytes), "GET", path, params=params)
        ) as frames:
            async for frame in frames:
                yield frame

    async def stats(self, container_id: str) -> AsyncGenerator[ContainerStats, None]:
        """Stream one stats object per frame until the container stops or the stream closes."""
        async with aclosing(self.stats_lines(container_id)) as lines:
            async for line in lines:
                yield self.parse_stats(line)

    async def stats_lines(self, container_id: str) -> AsyncGenerator[bytes, None]:
        """Stream the raw JSON line of each stats frame, none validated and none over 256 KiB.

        A caller that drops frames saves the cost of decoding them; `parse_stats` decodes one.
        """
        cid = self._http.segment(container_id)
        async with aclosing(
            decoded_stream(
                self._http,
                LineDecoder(STATS_FRAME_BYTES),
                "GET",
                f"/containers/{cid}/stats",
                params={"stream": True},
            )
        ) as lines:
            async for line in lines:
                yield line

    def parse_stats(self, line: bytes) -> ContainerStats:
        """Decode and validate one line of `stats_lines`; large lines belong in a worker thread."""
        try:
            document = json.loads(line)
        except (ValueError, RecursionError):
            raise EngineError(self._http.environment_id, "stats frame is not valid JSON") from None
        return self._http.parse(ContainerStats, document)

    async def stats_once(self, container_id: str) -> ContainerStats:
        """Fetch a single stats sample; the reply is capped at 1 MiB (ResponseTooLarge)."""
        cid = self._http.segment(container_id)
        return await self._http.get_parsed(
            ContainerStats,
            f"/containers/{cid}/stats",
            params={"stream": False, "one-shot": True},
            max_bytes=STATS_SAMPLE_BYTES,
        )


class ExecApi:
    """Exec instances created through `ContainersApi.create_exec`."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def start(self, exec_id: str) -> HijackedConnection:
        """Start an exec instance with a TTY over a hijacked, raw stream.

        The caller owns the connection and must close it, which hangs up the process's terminal.
        EngineError when the Engine answers with a multiplexed stream.
        """
        eid = self._http.segment(exec_id)
        connection = await self._http.hijack(
            f"/exec/{eid}/start", body=_encode_json({"Detach": False, "Tty": True})
        )
        return await _raw_stream(self._http, connection)

    async def resize(self, exec_id: str, rows: int, cols: int) -> None:
        """Resize the exec instance's terminal; rows 1-500, columns 1-1000, else EngineError."""
        eid = self._http.segment(exec_id)
        await self._http.request(
            "POST",
            f"/exec/{eid}/resize",
            params=_terminal_size(self._http, rows, cols),
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def inspect(self, exec_id: str) -> ExecInspect:
        """Fetch whether the instance runs and how it exited; the reply is capped at 64 KiB."""
        eid = self._http.segment(exec_id)
        return await self._http.get_parsed(
            ExecInspect, f"/exec/{eid}/json", max_bytes=EXEC_INSPECT_BYTES
        )


def _without_placeholders(entries: Any) -> Any:
    if not isinstance(entries, list):
        return entries
    return [e for e in entries if not (isinstance(e, str) and e in _NONE_PLACEHOLDERS)]


def _prepare_image_list(document: Any) -> Any:
    if not isinstance(document, list):
        return document
    prepared: list[Any] = []
    for item in document:
        if isinstance(item, dict):
            item = {
                **item,
                "RepoTags": _without_placeholders(item.get("RepoTags")),
                "RepoDigests": _without_placeholders(item.get("RepoDigests")),
                "Labels": None,
            }
        prepared.append(item)
    return prepared


def _prepare_history(document: Any) -> Any:
    return document[:MAX_HISTORY_ROWS] if isinstance(document, list) else document


def _decode_lines(lines: Sequence[bytes]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for line in lines:
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("progress line is not an object")
        objects.append(value)
    return objects


class ImagesApi:
    """Image listing, inspection, history, tagging, pulls, pushes and removal."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, all: bool = False, digests: bool = True) -> list[ImageSummary]:
        """List images; intermediate layers only when `all` is set.

        The reply is capped at 16 MiB and decoded in a worker thread. ResponseTooLarge past the
        cap; a partial list is never returned. Entries the Engine prints as `<none>:<none>` are
        dropped from the tags and digests, and labels are not read.
        """
        return await self._http.get_parsed_list(
            ImageSummary,
            "/images/json",
            params={"all": all, "digests": digests},
            max_bytes=IMAGE_LIST_BYTES,
            prepare=_prepare_image_list,
        )

    async def inspect(self, name: str) -> ImageInspect:
        """Fetch one image's detail by name, tag, digest or id; the reply is capped at 4 MiB.

        ResponseTooLarge past the cap.
        """
        ref = self._http.segment(name, image_ref=True)
        return await self._http.get_parsed(
            ImageInspect, f"/images/{ref}/json", max_bytes=IMAGE_INSPECT_BYTES
        )

    async def history(self, image_id: str) -> Sequence[ImageHistoryEntry]:
        """Fetch an image's layers, newest first, at most 512; the reply is capped at 1 MiB.

        ResponseTooLarge past the cap.
        """
        ref = self._http.segment(image_id, image_ref=True)
        return await self._http.get_parsed_list(
            ImageHistoryEntry,
            f"/images/{ref}/history",
            max_bytes=IMAGE_HISTORY_BYTES,
            prepare=_prepare_history,
        )

    async def tag(self, image_id: str, *, repository: str, tag: str) -> None:
        """Give an image another repository and tag."""
        ref = self._http.segment(image_id, image_ref=True)
        name = self._http.segment(repository, image_ref=True)
        if "@" in name or _IMAGE_TAG.fullmatch(tag) is None:
            raise EngineError(self._http.environment_id, "invalid resource identifier")
        await self._http.request(
            "POST",
            f"/images/{ref}/tag",
            params={"repo": name, "tag": tag},
            max_bytes=ACTION_REPLY_BYTES,
        )

    async def pull(
        self, image: str, *, tag: str | None = None, registry_auth: SecretStr | None = None
    ) -> AsyncGenerator[PullProgress, None]:
        """Pull an image, yielding progress; a failure the Engine reports mid-stream raises.

        `registry_auth` is the base64 `X-Registry-Auth` value. It is sent as a header and appears
        in no message or log line; a value outside the base64 alphabets is refused unsent. A
        progress line over 16 KiB, or a stream cut off mid-line, raises RequestOutcomeUnknown.
        """
        ref = self._http.segment(image, image_ref=True)
        params: dict[str, str | int | bool] = {"fromImage": ref}
        # Named with neither a tag nor a digest, the Engine pulls every tag of the repository.
        if tag is None and "@" not in ref and ":" not in ref.rpartition("/")[2]:
            tag = _DEFAULT_PULL_TAG
        if tag is not None:
            params["tag"] = self._http.segment(tag, image_ref=True)
        headers = self._auth_headers(registry_auth, always=False)
        async with aclosing(
            self._progress_lines("POST", "/images/create", params=params, headers=headers)
        ) as payloads:
            async for payload in payloads:
                if "error" in payload or "errorDetail" in payload:
                    # The registry's text can echo the request, so only the fact is reported.
                    raise EngineError(self._http.environment_id, "image pull failed")
                yield self._http.parse(PullProgress, payload)

    async def push(
        self, name: str, *, tag: str, registry_auth: SecretStr | None = None
    ) -> AsyncGenerator[PushProgress, None]:
        """Push a tag of a repository, yielding progress; a failure the Engine reports raises.

        The daemon refuses a push without credentials, so `X-Registry-Auth` is always sent and
        carries an empty document when `registry_auth` is None. The final `aux` object carries
        the pushed digest. Stream limits are those of `pull`.
        """
        repository = self._http.segment(name, image_ref=True)
        if "@" in repository or _IMAGE_TAG.fullmatch(tag) is None:
            raise EngineError(self._http.environment_id, "invalid resource identifier")
        headers = self._auth_headers(registry_auth, always=True)
        async with aclosing(
            self._progress_lines(
                "POST", f"/images/{repository}/push", params={"tag": tag}, headers=headers
            )
        ) as payloads:
            async for payload in payloads:
                if "error" in payload or "errorDetail" in payload:
                    raise EngineError(self._http.environment_id, "image push failed")
                yield self._http.parse(PushProgress, payload)

    async def remove(
        self, name: str, *, force: bool = False, noprune: bool = False
    ) -> tuple[ImageDeleteItem, ...]:
        """Remove an image or untag one reference, and return what the Engine says it did.

        The reply is capped at 1 MiB (ResponseTooLarge). `noprune` keeps untagged parent images.
        """
        ref = self._http.segment(name, image_ref=True)
        response = await self._http.request(
            "DELETE",
            f"/images/{ref}",
            params={"force": force, "noprune": noprune},
            timeouts=Timeouts.long(),
            max_bytes=IMAGE_REMOVE_REPLY_BYTES,
        )
        items = await asyncio.to_thread(self._removal_items, response)
        return tuple(items)

    def _removal_items(self, response: httpx.Response) -> Sequence[ImageDeleteItem]:
        try:
            document = json.loads(response.content)
        except (ValueError, RecursionError):
            raise EngineError(
                self._http.environment_id, "image removal reply is not valid JSON"
            ) from None
        return self._http.parse_list(ImageDeleteItem, document)

    def _auth_headers(
        self, registry_auth: SecretStr | None, *, always: bool
    ) -> dict[str, str] | None:
        return _registry_auth_headers(self._http, registry_auth, always=always)

    async def _progress_lines(
        self,
        method: str,
        path: str,
        *,
        params: Params,
        headers: Mapping[str, str] | None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield the JSON object of each progress line, off the event loop when a batch is large."""
        environment_id = self._http.environment_id
        decoder = LineDecoder(PROGRESS_LINE_BYTES)
        async with aclosing(
            self._http.stream_bytes(
                method, path, params=params, headers=headers, timeouts=Timeouts.long()
            )
        ) as chunks:
            failed = False
            try:
                async for chunk in chunks:
                    lines = decoder.feed(chunk)
                    if sum(len(line) for line in lines) > PROGRESS_THREADED_BYTES:
                        batch = await asyncio.to_thread(_decode_lines, lines)
                    else:
                        batch = _decode_lines(lines)
                    for payload in batch:
                        yield payload
                decoder.finish()
            except (FrameDecodeError, ValueError, RecursionError):
                failed = True
            if failed:
                raise RequestOutcomeUnknown(environment_id, f"{method} {path} stream is not valid")


def _prepare_volume_list(document: Any) -> Any:
    """Unwrap the `Volumes` envelope and bound each volume's labels; anything else is refused."""
    if not isinstance(document, dict):
        return {}
    return budget_volumes(document.get("Volumes"))


def _network_created_id(http: EngineHttp, response: httpx.Response) -> str:
    try:
        document = json.loads(response.content)
    except (ValueError, RecursionError):
        raise EngineError(http.environment_id, "network create reply is not valid JSON") from None
    reply = http.parse(_CreateReply, document)
    if _NETWORK_ID.fullmatch(reply.id) is None:
        raise EngineError(http.environment_id, "network create reply carries a malformed id")
    return reply.id


def _decode_volume(http: EngineHttp, response: httpx.Response) -> Volume:
    try:
        document = json.loads(response.content)
    except (ValueError, RecursionError):
        raise EngineError(http.environment_id, "volume create reply is not valid JSON") from None
    return http.parse(Volume, document)


def _only_keys(http: EngineHttp, document: object, allowed: frozenset[str], subject: str) -> None:
    if not isinstance(document, Mapping) or not document.keys() <= allowed:
        raise EngineError(http.environment_id, f"{subject} carries an option that is not sent")


def _string_map(http: EngineHttp, value: Mapping[str, str] | None, subject: str) -> dict[str, str]:
    mapping = {} if value is None else dict(value)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise EngineError(http.environment_id, f"{subject} must map strings to strings")
    return mapping


class NetworksApi:
    """Network listing, inspection, creation, attachment and removal."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[Network]:
        """List networks.

        The reply is capped at 16 MiB and decoded in a worker thread, with each network's labels
        cut to a fixed budget before validation. ResponseTooLarge past the cap; a partial list is
        never returned.
        """
        return await self._http.get_parsed_list(
            Network,
            "/networks",
            params=_filter_params(filters),
            max_bytes=NETWORK_LIST_BYTES,
            prepare=budget_networks,
        )

    async def count(self) -> int:
        """Count networks from a size-limited reply; ResponseTooLarge past the limit."""
        return await self._http.count_items("/networks")

    async def inspect(self, network_id: str) -> Network:
        """Fetch one network, including its attached containers; the reply is capped at 4 MiB.

        The Engine resolves the argument as an id, a name or an id prefix, so a caller that needs
        one of those checks the result against what it asked for. ResponseTooLarge past the cap.
        """
        nid = self._http.segment(network_id)
        return await self._http.get_parsed(
            Network, f"/networks/{nid}", max_bytes=NETWORK_INSPECT_BYTES
        )

    async def inspect_document(self, network_id: str) -> dict[str, Any]:
        """Fetch one network as the Engine sent it, capped at 4 MiB; the caller checks the `Id`."""
        nid = self._http.segment(network_id)
        response = await self._http.request(
            "GET", f"/networks/{nid}", max_bytes=NETWORK_INSPECT_BYTES
        )
        return await asyncio.to_thread(_json_object, self._http, response.content, "network")

    async def create(self, body: Mapping[str, Any]) -> str:
        """Create a network from an Engine create body and return its id.

        `body` may carry only Name, Driver, Internal, Attachable, EnableIPv6, IPAM, Options and
        Labels; any other key is refused unsent. EngineError when the returned id is neither 64
        lower-case hex digits nor 25 lower-case base-36 characters. The reply is capped at 64 KiB
        (ResponseTooLarge). RequestOutcomeUnknown means the network may exist.
        """
        _only_keys(self._http, body, _NETWORK_CREATE_KEYS, "network create body")
        name = body.get("Name")
        if not isinstance(name, str) or _RESOURCE_NAME.fullmatch(name) is None:
            raise EngineError(self._http.environment_id, "invalid resource identifier")
        encoded = await asyncio.to_thread(_encode_json, body)
        response = await self._http.request(
            "POST",
            "/networks/create",
            content=encoded,
            headers={"Content-Type": "application/json"},
            max_bytes=NETWORK_WRITE_REPLY_BYTES,
        )
        return _network_created_id(self._http, response)

    async def connect(self, network_id: str, body: Mapping[str, Any]) -> None:
        """Attach a container to a network.

        `body` is {"Container": id, "EndpointConfig": {"Aliases": [...], "IPAMConfig":
        {"IPv4Address": ..., "IPv6Address": ...}}} with EndpointConfig optional; any other key is
        refused unsent.
        """
        nid = self._http.segment(network_id)
        _only_keys(self._http, body, _CONNECT_KEYS, "connect body")
        container = body.get("Container")
        if not isinstance(container, str):
            raise EngineError(self._http.environment_id, "connect body names no container")
        self._http.segment(container)
        endpoint = body.get("EndpointConfig")
        if endpoint is not None:
            _only_keys(self._http, endpoint, _ENDPOINT_CONFIG_KEYS, "endpoint configuration")
            ipam = endpoint.get("IPAMConfig")
            if ipam is not None:
                _only_keys(self._http, ipam, _ENDPOINT_IPAM_KEYS, "endpoint address")
        encoded = await asyncio.to_thread(_encode_json, body)
        await self._http.request(
            "POST",
            f"/networks/{nid}/connect",
            content=encoded,
            headers={"Content-Type": "application/json"},
            max_bytes=NETWORK_WRITE_REPLY_BYTES,
        )

    async def disconnect(self, network_id: str, *, container_id: str) -> None:
        """Detach a container from a network; the Engine is never asked to force it."""
        nid = self._http.segment(network_id)
        cid = self._http.segment(container_id)
        await self._http.request(
            "POST",
            f"/networks/{nid}/disconnect",
            content=_encode_json({"Container": cid, "Force": False}),
            headers={"Content-Type": "application/json"},
            max_bytes=NETWORK_WRITE_REPLY_BYTES,
        )

    async def remove(self, network_id: str) -> None:
        """Remove a network."""
        nid = self._http.segment(network_id)
        await self._http.request("DELETE", f"/networks/{nid}", max_bytes=ACTION_REPLY_BYTES)


class VolumesApi:
    """Volume listing, inspection, creation and removal."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[Volume]:
        """List volumes.

        The reply is capped at 16 MiB and decoded in a worker thread, with each volume's labels
        cut to a fixed budget before validation. ResponseTooLarge past the cap; a partial list is
        never returned.
        """
        return await self._http.get_parsed_list(
            Volume,
            "/volumes",
            params=_filter_params(filters),
            max_bytes=VOLUME_LIST_BYTES,
            prepare=_prepare_volume_list,
        )

    async def count(self) -> int:
        """Count volumes from a size-limited reply; ResponseTooLarge past the limit."""
        return await self._http.count_items("/volumes", key="Volumes")

    async def inspect(self, name: str) -> Volume:
        """Fetch one volume; the reply is capped at 4 MiB (ResponseTooLarge)."""
        vol = self._http.segment(name)
        return await self._http.get_parsed(
            Volume, f"/volumes/{vol}", max_bytes=VOLUME_INSPECT_BYTES
        )

    async def create(
        self,
        *,
        name: str,
        driver: str = "local",
        driver_opts: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> Volume:
        """Create a volume and return what the Engine reports.

        The Engine answers an existing name of the same driver with that volume, not an error,
        so a caller that must not adopt one checks first. The reply is capped at 1 MiB
        (ResponseTooLarge). RequestOutcomeUnknown means the volume may exist.
        """
        environment_id = self._http.environment_id
        if _RESOURCE_NAME.fullmatch(name) is None:
            raise EngineError(environment_id, "invalid resource identifier")
        if _VOLUME_DRIVER.fullmatch(driver) is None:
            raise EngineError(environment_id, "volume driver name is malformed")
        body: dict[str, Any] = {"Name": name, "Driver": driver}
        options = _string_map(self._http, driver_opts, "volume driver options")
        if options:
            body["DriverOpts"] = options
        tags = _string_map(self._http, labels, "volume labels")
        if tags:
            body["Labels"] = tags
        encoded = await asyncio.to_thread(_encode_json, body)
        response = await self._http.request(
            "POST",
            "/volumes/create",
            content=encoded,
            headers={"Content-Type": "application/json"},
            max_bytes=VOLUME_CREATE_REPLY_BYTES,
        )
        return await asyncio.to_thread(_decode_volume, self._http, response)

    async def remove(self, name: str, *, force: bool = False) -> None:
        """Remove a volume."""
        vol = self._http.segment(name)
        await self._http.request(
            "DELETE", f"/volumes/{vol}", params={"force": force}, max_bytes=ACTION_REPLY_BYTES
        )


class SystemApi:
    """Host information and the events stream."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def info(self) -> SystemInfo:
        """Fetch host-level facts."""
        return await self._http.get_parsed(SystemInfo, "/info", max_bytes=INFO_RESPONSE_BYTES)

    async def image_disk_usage(self) -> ImageDiskUsage:
        """Fetch the bytes the host's images occupy, each shared layer counted once.

        Asks for image data only (`type=image`). The reply is capped at 16 MiB and decoded in a
        worker thread. ResponseTooLarge past the cap.
        """
        return await self._http.get_parsed(
            ImageDiskUsage,
            "/system/df",
            params={"type": "image"},
            max_bytes=DISK_USAGE_BYTES,
        )

    def events(
        self,
        *,
        since: int | None = None,
        filters: Filters | None = None,
        backoff: BackoffPolicy | None = None,
        replay_seconds: int | None = None,
    ) -> AsyncGenerator[EngineEvent, None]:
        """Stream events, reconnecting with backoff until the client closes.

        `replay_seconds` starts the first connection that far before now, which gives a reconnect
        before the first event a point to resume from; it excludes `since`. Wrap in
        `contextlib.aclosing` to release the stream early.
        """
        if replay_seconds is not None:
            if since is not None or replay_seconds < 0:
                raise ValueError("replay_seconds excludes since and is not negative")
            since = time.time_ns() // 1_000_000_000 - replay_seconds
        return watch_events(self._http, since=since, filters=filters, backoff=backoff)


def _swarm_id(http: EngineHttp, value: str) -> str:
    if not isinstance(value, str) or _SWARM_ID.fullmatch(value) is None:
        raise EngineError(http.environment_id, "invalid resource identifier")
    return value


def _version(http: EngineHttp, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise EngineError(http.environment_id, "object version is out of range")
    return value


def _json_object(http: EngineHttp, content: bytes, subject: str) -> dict[str, Any]:
    try:
        document = json.loads(content)
    except (ValueError, RecursionError):
        raise EngineError(http.environment_id, f"{subject} reply is not valid JSON") from None
    if not isinstance(document, dict):
        raise EngineError(http.environment_id, f"{subject} reply is not an object")
    return document


def _swarm_created(http: EngineHttp, response: httpx.Response, subject: str) -> _SwarmCreateReply:
    reply = http.parse(_SwarmCreateReply, _json_object(http, response.content, subject))
    if _SWARM_ID.fullmatch(reply.id) is None:
        raise EngineError(http.environment_id, f"{subject} reply carries a malformed id")
    return reply


def _raw_object(
    http: EngineHttp, response: httpx.Response, subject: str
) -> tuple[str, int, dict[str, Any], dict[str, Any] | None]:
    document = _json_object(http, response.content, subject)
    identifier = document.get("ID")
    spec = document.get("Spec")
    previous = document.get("PreviousSpec")
    if (
        not isinstance(identifier, str)
        or not identifier
        or not isinstance(spec, dict)
        or not (previous is None or isinstance(previous, dict))
    ):
        raise EngineError(http.environment_id, f"{subject} reply did not match the expected shape")
    return identifier, http.parse(ObjectVersion, document.get("Version")).index, spec, previous


def _decode_service_document(http: EngineHttp, response: httpx.Response) -> ServiceDocument:
    identifier, version, spec, previous = _raw_object(http, response, "service")
    return ServiceDocument(identifier, version, spec, previous)


def _decode_node_document(http: EngineHttp, response: httpx.Response) -> NodeDocument:
    identifier, version, spec, _ = _raw_object(http, response, "node")
    return NodeDocument(identifier, version, spec)


def _decode_config(http: EngineHttp, response: httpx.Response) -> ConfigDocument:
    document = _json_object(http, response.content, "config")
    spec = document.get("Spec")
    encoded = spec.get("Data") if isinstance(spec, dict) else None
    config = http.parse(SwarmConfig, budget_spec_object(document))
    if encoded is None:
        return ConfigDocument(config, b"")
    if not isinstance(encoded, str):
        raise EngineError(http.environment_id, "config content is not base64")
    try:
        return ConfigDocument(config, base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError):
        raise EngineError(http.environment_id, "config content is not base64") from None


def _decode_distribution(http: EngineHttp, response: httpx.Response) -> DistributionDescriptor:
    return http.parse(DistributionDescriptor, _json_object(http, response.content, "distribution"))


def _object_body(name: str, data: bytes, labels: dict[str, str]) -> bytes:
    body: dict[str, Any] = {"Name": name, "Data": base64.b64encode(data).decode("ascii")}
    if labels:
        body["Labels"] = labels
    return _encode_json(body)


async def _create_object(
    http: EngineHttp,
    path: str,
    subject: str,
    *,
    name: str,
    data: bytes,
    labels: Mapping[str, str] | None,
    max_bytes: int,
) -> str:
    if not isinstance(name, str) or _SWARM_OBJECT_NAME.fullmatch(name) is None:
        raise EngineError(http.environment_id, "invalid resource identifier")
    if not isinstance(data, bytes) or not 0 < len(data) <= max_bytes:
        raise EngineError(http.environment_id, f"{subject} content size is out of range")
    tags = _string_map(http, labels, f"{subject} labels")
    encoded = await asyncio.to_thread(_object_body, name, data, tags)
    response = await http.request(
        "POST",
        path,
        content=encoded,
        headers={"Content-Type": "application/json"},
        max_bytes=SWARM_WRITE_REPLY_BYTES,
    )
    return _swarm_created(http, response, f"{subject} create").id


def _canonical_id_filter(
    http: EngineHttp, filters: Filters | None, key: str, subject: str
) -> frozenset[str] | None:
    if not filters or key not in filters:
        return None
    values = filters[key]
    if not values or not all(isinstance(v, str) and _SWARM_ID.fullmatch(v) for v in values):
        raise EngineError(http.environment_id, f"{subject} filter must name ids")
    return frozenset(values)


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


def _canonical_entries[T: _HasId](items: list[T]) -> list[T]:
    return [item for item in items if _SWARM_ID.fullmatch(item.id) is not None]


def _raw_framing(content_type: str | None, tty_hint: bool) -> bool:
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if kind == _RAW_STREAM:
        return True
    if kind == _MULTIPLEXED_STREAM:
        return False
    return tty_hint


async def _framed_log_stream(
    http: EngineHttp,
    path: str,
    params: Params,
    *,
    tty_hint: bool,
    max_frame_bytes: int,
) -> AsyncGenerator[LogFrame, None]:
    """Yield log frames, choosing raw or multiplexed framing from the response's Content-Type.

    `tty_hint` applies only when the response names neither framing.
    """
    raw = tty_hint

    def choose(headers: Mapping[str, str]) -> None:
        nonlocal raw
        raw = _raw_framing(headers.get("content-type"), tty_hint)

    decoder = LogFrameDecoder(max_frame_bytes)
    async with aclosing(http.stream_bytes("GET", path, params=params, on_headers=choose)) as chunks:
        try:
            async for chunk in chunks:
                if raw:
                    for start in range(0, len(chunk), max_frame_bytes):
                        yield LogFrame("stdout", chunk[start : start + max_frame_bytes])
                else:
                    for frame in decoder.feed(chunk):
                        yield frame
            if not raw:
                decoder.finish()
        except TruncatedStream as exc:
            raise EnvironmentUnreachable(http.environment_id, str(exc)) from None
        except FrameDecodeError as exc:
            raise EngineError(http.environment_id, str(exc)) from None


class SwarmApi:
    """Swarm identity and counts; a daemon that is not a manager fails each with EngineError."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def inspect(self) -> SwarmCluster:
        """Fetch the cluster identity; the reply is capped at 1 MiB (ResponseTooLarge).

        The join tokens in the reply are never modelled.
        """
        return await self._http.get_parsed(SwarmCluster, "/swarm", max_bytes=SWARM_INSPECT_BYTES)

    async def count_nodes(self) -> int:
        """Count nodes from a size-limited reply; ResponseTooLarge past the limit."""
        return await self._http.count_items("/nodes")

    async def count_services(self) -> int:
        """Count services from a size-limited reply; ResponseTooLarge past the limit."""
        return await self._http.count_items("/services")

    async def stack_namespaces(self) -> frozenset[str]:
        """Return the case-folded stack namespaces that label a service.

        The Engine filters on the label key alone, the reply is capped at 4 MiB and decoded in a
        worker thread with the label budget applied, and only the namespace label is read.
        ResponseTooLarge past the cap; a partial set is never returned.
        """
        services = await self._http.get_parsed_list(
            _LabelledService,
            "/services",
            params=_filter_params({"label": [_SWARM_NAMESPACE_LABEL]}),
            max_bytes=STACK_NAMESPACES_BYTES,
            prepare=budget_services,
        )
        return frozenset(
            value.casefold()
            for service in services
            for key, value in (service.spec.labels or {}).items()
            if key == _SWARM_NAMESPACE_LABEL and value
        )


class ServicesApi:
    """Service listing, inspection, creation, update, removal and logs."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[SwarmService]:
        """List services with their task counts.

        The reply is capped at 16 MiB and decoded in a worker thread, with the labels of each
        spec and container spec cut to a fixed budget before validation. ResponseTooLarge past the
        cap; a partial list is never returned.
        """
        return _canonical_entries(
            await self._http.get_parsed_list(
                SwarmService,
                "/services",
                params=_filter_params(filters, status=True),
                max_bytes=SERVICE_LIST_BYTES,
                prepare=budget_services,
            )
        )

    async def inspect(self, service_id: str) -> SwarmService:
        """Fetch one service for display; the reply is capped at 4 MiB (ResponseTooLarge).

        The Engine resolves the argument as an id, a name or an id prefix, so a caller that needs
        one of those checks the returned `id` against what it asked for. Labels are budgeted, so
        the result is never the basis of an update; use `document` for that.
        """
        sid = self._http.segment(service_id)
        return await self._http.get_parsed(
            SwarmService,
            f"/services/{sid}",
            max_bytes=SERVICE_INSPECT_BYTES,
            prepare=budget_service,
        )

    async def document(self, service_id: str) -> ServiceDocument:
        """Fetch one service's raw spec and previous spec, unbudgeted, for an update.

        The reply is capped at 4 MiB (ResponseTooLarge) and decoded in a worker thread. Every
        field of both specs is kept, unknown ones included. The `id` is the Engine's, to be
        checked by the caller as for `inspect`.
        """
        sid = self._http.segment(service_id)
        response = await self._http.request(
            "GET", f"/services/{sid}", max_bytes=SERVICE_INSPECT_BYTES
        )
        return await asyncio.to_thread(_decode_service_document, self._http, response)

    async def create(self, spec: Mapping[str, Any], *, registry_auth: SecretStr | None) -> str:
        """Create a service from a spec and return its id.

        `spec` may carry only Name, Labels, TaskTemplate, Mode, UpdateConfig, RollbackConfig and
        EndpointSpec, and its Name must be a valid service name; anything else is refused unsent.
        `registry_auth` is the base64 `X-Registry-Auth` value, sent only when given, never in a
        message or log line; a value outside the base64 alphabets is refused unsent. The body is
        encoded in a worker thread and the reply is capped at 64 KiB (ResponseTooLarge).
        EngineError when the returned id is not 25 lower-case base-36 characters. The Engine's
        warnings are counted in a debug log line and dropped. RequestOutcomeUnknown means the
        service may exist.
        """
        _only_keys(self._http, spec, _SERVICE_SPEC_KEYS, "service spec")
        name = spec.get("Name")
        if not isinstance(name, str) or _SERVICE_NAME.fullmatch(name) is None:
            raise EngineError(self._http.environment_id, "invalid resource identifier")
        headers = {"Content-Type": "application/json"}
        headers.update(_registry_auth_headers(self._http, registry_auth, always=False) or {})
        encoded = await asyncio.to_thread(_encode_json, spec)
        response = await self._http.request(
            "POST",
            "/services/create",
            content=encoded,
            headers=headers,
            timeouts=Timeouts.long(),
            max_bytes=SWARM_WRITE_REPLY_BYTES,
        )
        reply = _swarm_created(self._http, response, "service create")
        if reply.warnings:
            logger.debug(
                "service_create_warnings",
                extra={"environment_id": self._http.environment_id, "count": len(reply.warnings)},
            )
        return reply.id

    async def update(
        self,
        service_id: str,
        spec: Mapping[str, Any],
        *,
        version: int,
        registry_auth: SecretStr | None = None,
        registry_auth_from: Literal["spec", "previous-spec"] | None = None,
        rollback: bool = False,
    ) -> None:
        """Replace a service's spec, provided its version is still `version`.

        `service_id` must be a 25-character Swarm id, so a name or prefix can never resolve to
        another service. `spec` may carry only the keys `create` accepts. With `rollback` the
        Engine restores the previous spec and ignores the body's content, which must still be a
        spec. `registry_auth` and `registry_auth_from` are exclusive; the first is sent as
        `X-Registry-Auth` as for `create`, the second asks the Engine to reuse the credentials
        it holds. With neither, the empty auth document is sent: an update that names no
        credential source must not inherit the ones stored in the spec. Docker answers a stale
        version with HTTP 500, which arrives as an EngineError with that status code. The reply is
        capped at 64 KiB (ResponseTooLarge). RequestOutcomeUnknown means the update may have been
        applied.
        """
        sid = _swarm_id(self._http, service_id)
        _only_keys(self._http, spec, _SERVICE_SPEC_KEYS, "service spec")
        params: dict[str, str | int | bool] = {"version": _version(self._http, version)}
        if registry_auth is not None and registry_auth_from is not None:
            raise EngineError(self._http.environment_id, "registry credential source is ambiguous")
        if registry_auth_from is not None:
            if registry_auth_from not in _REGISTRY_AUTH_SOURCES:
                raise EngineError(
                    self._http.environment_id, "registry credential source is unknown"
                )
            params["registryAuthFrom"] = registry_auth_from
        if rollback:
            params["rollback"] = "previous"
        headers = {"Content-Type": "application/json"}
        headers.update(
            _registry_auth_headers(self._http, registry_auth, always=registry_auth_from is None)
            or {}
        )
        encoded = await asyncio.to_thread(_encode_json, spec)
        await self._http.request(
            "POST",
            f"/services/{sid}/update",
            params=params,
            content=encoded,
            headers=headers,
            timeouts=Timeouts.long(),
            max_bytes=SWARM_WRITE_REPLY_BYTES,
        )

    async def remove(self, service_id: str) -> None:
        """Remove a service; `service_id` must be a 25-character Swarm id."""
        sid = _swarm_id(self._http, service_id)
        await self._http.request("DELETE", f"/services/{sid}", max_bytes=SWARM_WRITE_REPLY_BYTES)

    async def logs(
        self,
        service_id: str,
        *,
        tty: bool,
        follow: bool = True,
        stdout: bool = True,
        stderr: bool = True,
        since: int | str | None = None,
        until: int | str | None = None,
        tail: int | None = None,
        max_frame_bytes: int = LOG_FRAME_BYTES,
    ) -> AsyncGenerator[LogFrame, None]:
        """Stream a service's output across its tasks as frames, each line carrying its details.

        Timestamps and details are always requested. The response's Content-Type decides whether
        frames are multiplexed or raw; `tty` is the fallback when it names neither. `since` and
        `until` are Unix seconds or `seconds.nanos` text (see `format_engine_timestamp`). `tail`
        is applied by the Engine per task. No frame is larger than `max_frame_bytes`: a
        multiplexed frame declaring more ends the stream with EngineError, and a raw chunk is
        split. Wrap in `contextlib.aclosing` to release the stream early.
        """
        sid = self._http.segment(service_id)
        if max_frame_bytes < 1:
            raise EngineError(self._http.environment_id, "log frame limit is not positive")
        params = _swarm_log_params(
            self._http,
            follow=follow,
            stdout=stdout,
            stderr=stderr,
            since=since,
            until=until,
            tail=tail,
        )
        async with aclosing(
            _framed_log_stream(
                self._http,
                f"/services/{sid}/logs",
                params,
                tty_hint=tty,
                max_frame_bytes=max_frame_bytes,
            )
        ) as frames:
            async for frame in frames:
                yield frame


class TasksApi:
    """Task listing, inspection and logs."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[SwarmTask]:
        """List tasks, including the history Docker retains.

        A `service` or `node` filter must name 25-character Swarm ids, at least one, and the
        result keeps only tasks whose `service_id` or `node_id` is one of them: the Engine
        resolves names and prefixes in these filters, so its match is not exact. An entry whose
        id is not 25 lower-case base-36 characters is dropped. A removed service is NotFound. The
        reply is capped at 16 MiB and decoded in a worker thread; ResponseTooLarge past the cap,
        and a partial list is never returned.
        """
        services = _canonical_id_filter(self._http, filters, "service", "task service")
        nodes = _canonical_id_filter(self._http, filters, "node", "task node")
        tasks = _canonical_entries(
            await self._http.get_parsed_list(
                SwarmTask, "/tasks", params=_filter_params(filters), max_bytes=TASK_LIST_BYTES
            )
        )
        return [
            task
            for task in tasks
            if (services is None or task.service_id in services)
            and (nodes is None or task.node_id in nodes)
        ]

    async def inspect(self, task_id: str) -> SwarmTask:
        """Fetch one task; the reply is capped at 1 MiB (ResponseTooLarge).

        The Engine resolves the argument as an id, a name or an id prefix, so a caller that needs
        one of those checks the returned `id` against what it asked for.
        """
        tid = self._http.segment(task_id)
        return await self._http.get_parsed(SwarmTask, f"/tasks/{tid}", max_bytes=TASK_INSPECT_BYTES)

    async def logs(
        self,
        task_id: str,
        *,
        tty: bool,
        follow: bool = True,
        stdout: bool = True,
        stderr: bool = True,
        since: int | str | None = None,
        until: int | str | None = None,
        tail: int | None = None,
        max_frame_bytes: int = LOG_FRAME_BYTES,
    ) -> AsyncGenerator[LogFrame, None]:
        """Stream one task's output as frames, with the same options and framing as service logs."""
        tid = self._http.segment(task_id)
        if max_frame_bytes < 1:
            raise EngineError(self._http.environment_id, "log frame limit is not positive")
        params = _swarm_log_params(
            self._http,
            follow=follow,
            stdout=stdout,
            stderr=stderr,
            since=since,
            until=until,
            tail=tail,
        )
        async with aclosing(
            _framed_log_stream(
                self._http,
                f"/tasks/{tid}/logs",
                params,
                tty_hint=tty,
                max_frame_bytes=max_frame_bytes,
            )
        ) as frames:
            async for frame in frames:
                yield frame


class NodesApi:
    """Node listing, inspection and availability changes."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self) -> list[SwarmNode]:
        """List nodes; the reply is capped at 4 MiB and decoded in a worker thread.

        An entry whose id is not 25 lower-case base-36 characters is dropped.
        """
        return _canonical_entries(
            await self._http.get_parsed_list(SwarmNode, "/nodes", max_bytes=NODE_LIST_BYTES)
        )

    async def inspect(self, node_id: str) -> SwarmNode:
        """Fetch one node; the reply is capped at 1 MiB (ResponseTooLarge).

        The Engine resolves the argument as an id, a hostname or an id prefix, so a caller that
        needs one of those checks the returned `id` against what it asked for.
        """
        nid = self._http.segment(node_id)
        return await self._http.get_parsed(SwarmNode, f"/nodes/{nid}", max_bytes=NODE_INSPECT_BYTES)

    async def document(self, node_id: str) -> NodeDocument:
        """Fetch one node's raw spec for an update; the reply is capped at 1 MiB."""
        nid = self._http.segment(node_id)
        response = await self._http.request("GET", f"/nodes/{nid}", max_bytes=NODE_INSPECT_BYTES)
        return await asyncio.to_thread(_decode_node_document, self._http, response)

    async def update(self, node_id: str, spec: Mapping[str, Any], *, version: int) -> None:
        """Replace a node's spec, provided its version is still `version`.

        `node_id` must be a 25-character Swarm id. `spec` may carry only Name, Labels, Role and
        Availability. Docker answers a stale version with HTTP 500, which arrives as an
        EngineError with that status code. RequestOutcomeUnknown means the update may have
        been applied.
        """
        nid = _swarm_id(self._http, node_id)
        _only_keys(self._http, spec, _NODE_SPEC_KEYS, "node spec")
        encoded = await asyncio.to_thread(_encode_json, spec)
        await self._http.request(
            "POST",
            f"/nodes/{nid}/update",
            params={"version": _version(self._http, version)},
            content=encoded,
            headers={"Content-Type": "application/json"},
            max_bytes=SWARM_WRITE_REPLY_BYTES,
        )


class ConfigsApi:
    """Config listing, inspection, creation and removal."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[SwarmConfig]:
        """List configs without their content.

        The reply is capped at 32 MiB and decoded in a worker thread, where each config's
        content is dropped and its labels are cut to a fixed budget before validation.
        ResponseTooLarge past the cap; a partial list is never returned. An entry whose id is not
        25 lower-case base-36 characters is dropped.
        """
        return _canonical_entries(
            await self._http.get_parsed_list(
                SwarmConfig,
                "/configs",
                params=_filter_params(filters),
                max_bytes=CONFIG_LIST_BYTES,
                prepare=budget_spec_objects,
            )
        )

    async def inspect(self, config_id: str) -> ConfigDocument:
        """Fetch one config with its decoded content; the reply is capped at 4 MiB.

        Decoded in a worker thread (ResponseTooLarge past the cap). The Engine resolves the
        argument as an id, a name or an id prefix, so a caller that needs one of those checks
        the returned `config.id` against what it asked for. EngineError when the content is not
        base64.
        """
        cid = self._http.segment(config_id)
        response = await self._http.request(
            "GET", f"/configs/{cid}", max_bytes=CONFIG_INSPECT_BYTES
        )
        return await asyncio.to_thread(_decode_config, self._http, response)

    async def create(self, *, name: str, data: bytes, labels: Mapping[str, str] | None) -> str:
        """Create a config and return its id.

        The body carries only Name, Labels and Data (never a driver or templating). `data` must
        be 1 to 1,023,999 bytes and `name` a valid config name, else EngineError unsent. The
        reply is capped at 64 KiB (ResponseTooLarge). EngineError when the returned id is not 25
        lower-case base-36 characters. RequestOutcomeUnknown means the config may exist.
        """
        return await _create_object(
            self._http,
            "/configs/create",
            "config",
            name=name,
            data=data,
            labels=labels,
            max_bytes=_CONFIG_DATA_MAX_BYTES,
        )

    async def remove(self, config_id: str) -> None:
        """Remove a config; `config_id` must be a 25-character Swarm id."""
        cid = _swarm_id(self._http, config_id)
        await self._http.request("DELETE", f"/configs/{cid}", max_bytes=SWARM_WRITE_REPLY_BYTES)


class SecretsApi:
    """Secret listing, inspection, creation and removal. No call returns a secret's value."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def list(self, *, filters: Filters | None = None) -> list[SwarmSecret]:
        """List secrets.

        The reply is capped at 16 MiB and decoded in a worker thread, where any `Spec.Data` a
        daemon sends is dropped and labels are cut to a fixed budget before validation.
        ResponseTooLarge past the cap; a partial list is never returned. An entry whose id is not
        25 lower-case base-36 characters is dropped.
        """
        return _canonical_entries(
            await self._http.get_parsed_list(
                SwarmSecret,
                "/secrets",
                params=_filter_params(filters),
                max_bytes=SECRET_LIST_BYTES,
                prepare=budget_spec_objects,
            )
        )

    async def inspect(self, secret_id: str) -> SwarmSecret:
        """Fetch one secret's metadata; the reply is capped at 1 MiB (ResponseTooLarge).

        The Engine resolves the argument as an id, a name or an id prefix, so a caller that needs
        one of those checks the returned `id` against what it asked for.
        """
        sid = self._http.segment(secret_id)
        return await self._http.get_parsed(
            SwarmSecret,
            f"/secrets/{sid}",
            max_bytes=SECRET_INSPECT_BYTES,
            prepare=budget_spec_object,
        )

    async def create(self, *, name: str, data: bytes, labels: Mapping[str, str] | None) -> str:
        """Create a secret and return its id.

        The body carries only Name, Labels and Data (never a driver or templating). `data` must
        be 1 to 511,999 bytes and `name` a valid secret name, else EngineError unsent. `data`
        appears in no exception, log line or repr. The reply is capped at 64 KiB
        (ResponseTooLarge). EngineError when the returned id is not 25 lower-case base-36
        characters. RequestOutcomeUnknown means the secret may exist.
        """
        return await _create_object(
            self._http,
            "/secrets/create",
            "secret",
            name=name,
            data=data,
            labels=labels,
            max_bytes=_SECRET_DATA_MAX_BYTES,
        )

    async def remove(self, secret_id: str) -> None:
        """Remove a secret; `secret_id` must be a 25-character Swarm id."""
        sid = _swarm_id(self._http, secret_id)
        await self._http.request("DELETE", f"/secrets/{sid}", max_bytes=SWARM_WRITE_REPLY_BYTES)


class DistributionApi:
    """Registry lookups made by the daemon, which works on a worker as well as a manager."""

    def __init__(self, http: EngineHttp) -> None:
        self._http = http

    async def inspect(
        self, reference: str, *, registry_auth: SecretStr | None
    ) -> DistributionDescriptor:
        """Resolve an image reference to its content digest at its registry.

        `registry_auth` is the base64 `X-Registry-Auth` value, sent only when given, never in a
        message or log line; a value outside the base64 alphabets is refused unsent. The reply is
        capped at 1 MiB (ResponseTooLarge). EngineError when the digest is not `sha256:` and 64
        lower-case hex digits.
        """
        ref = self._http.segment(reference, image_ref=True)
        headers = _registry_auth_headers(self._http, registry_auth, always=False)
        response = await self._http.request(
            "GET", f"/distribution/{ref}/json", headers=headers, max_bytes=DISTRIBUTION_BYTES
        )
        return await asyncio.to_thread(_decode_distribution, self._http, response)
