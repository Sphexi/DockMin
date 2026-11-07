"""Runs `docker-buildx build` against one environment and reads its raw JSON progress."""

import asyncio
import base64
import binascii
import json
import re
import shutil
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from app.compose_cli.binary import ComposeBinary, usable
from app.compose_cli.environment import build_environment, is_reserved
from app.compose_cli.errors import ComposeError, ComposeUnavailable
from app.compose_cli.host_check import check_target_host
from app.compose_cli.parsers import MAX_EVENT_TEXT_CHARS
from app.compose_cli.process import (
    Invocation,
    drive,
    pump,
    release,
    spawn,
    uninterrupted,
)
from app.compose_cli.target import ComposeTarget, materialise
from app.compose_cli.workdir import populate_invocation_dir
from app.logging import get_logger
from app.netcheck import Resolver, system_resolver

logger = get_logger(__name__)

BuildxBinary = ComposeBinary

SEARCH_PATHS: Final[tuple[Path, ...]] = (
    Path("/usr/local/libexec/docker/cli-plugins/docker-buildx"),
    Path("/usr/libexec/docker/cli-plugins/docker-buildx"),
    Path("/usr/local/lib/docker/cli-plugins/docker-buildx"),
    Path("/usr/lib/docker/cli-plugins/docker-buildx"),
)
PATH_NAME: Final = "docker-buildx"
BUILDX_CONFIG_DIRECTORY: Final = "buildx"
BUILD_TIMEOUT_SECONDS: Final = 30 * 60.0
MAX_CONCURRENT_BUILDS: Final = 2
MIN_BUILD_SECONDS: Final = 1.0
MAX_LOG_BYTES: Final = 1024 * 1024
MAX_PROGRESS_LINE_BYTES: Final = 2 * 1024 * 1024
MAX_LOG_LINE_CHARS: Final = 64 * 1024
MAX_VERTICES: Final = 2048
MAX_STDERR_LINES: Final = 200
MAX_STDERR_BYTES: Final = 64 * 1024

_UNTRUSTED_EVENT: Final = "buildx_binary_untrusted"
_MAX_TAG_CHARS: Final = 255
_MAX_DIGEST_CHARS: Final = 160
_TAG: Final = re.compile(
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
)
_STAGE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ARGUMENT_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_LABEL_KEY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
_DIGEST: Final = re.compile(r"[a-z0-9]+:[0-9a-f]{32,128}")
_LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_STATUS_KEYS: Final = ("vertexes", "statuses", "logs", "warnings")

type VertexState = Literal["waiting", "running", "done", "error"]


class BuildxError(ComposeError):
    """Base class for every error the buildx wrapper raises."""

    reason = "build_failed"


class BuildxUnavailable(BuildxError):
    """No usable buildx binary exists on this server."""

    reason = "buildx_unavailable"

    def __init__(self) -> None:
        super().__init__("Docker buildx was not found on this server.")


class BuildFailed(BuildxError):
    """buildx exited non-zero; `log` and `stderr` are scrubbed."""

    def __init__(
        self,
        exit_code: int | None,
        stderr: str,
        log: str,
        vertices: "tuple[VertexUpdate, ...]",
        *,
        message: str | None = None,
    ) -> None:
        super().__init__(message or f"The image build exited with status {exit_code}.")
        self.exit_code = exit_code
        self.stderr = stderr
        self.log = log
        self.vertices = vertices

    def __repr__(self) -> str:
        return f"{type(self).__name__}(exit_code={self.exit_code!r}, vertices={len(self.vertices)})"


class BuildTimedOut(BuildFailed):
    """A build was killed after running past its deadline."""

    def __init__(self, stderr: str, log: str, vertices: "tuple[VertexUpdate, ...]") -> None:
        super().__init__(
            None, stderr, log, vertices, message="The image build did not finish in time."
        )


@dataclass(frozen=True)
class VertexUpdate:
    """One build step as BuildKit last reported it."""

    digest: str
    name: str
    status: VertexState
    cached: bool
    error: str | None


@dataclass(frozen=True)
class LogLine:
    """One line of a build step's output, scrubbed and cut for display."""

    vertex: str
    text: str


type BuildEvent = VertexUpdate | LogLine


@dataclass(frozen=True)
class BuildSpec:
    """What to build; every field is checked before it can reach the argument list."""

    tag: str
    context_dir: Path
    dockerfile: str = "Dockerfile"
    target: str | None = None
    no_cache: bool = False
    args: Mapping[str, str] = field(default_factory=dict, repr=False)
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.tag) > _MAX_TAG_CHARS or _TAG.fullmatch(self.tag) is None:
            raise ValueError("The image tag is outside the permitted grammar.")
        if not self.context_dir.is_absolute():
            raise ValueError("The build context must be an absolute path.")
        if not _is_inner_path(self.dockerfile):
            raise ValueError("The Dockerfile path must be relative and stay inside the context.")
        if self.target is not None and _STAGE.fullmatch(self.target) is None:
            raise ValueError("The build target is outside the permitted grammar.")
        for name, value in self.args.items():
            if _ARGUMENT_NAME.fullmatch(name) is None or is_reserved(name):
                raise ValueError("A build argument name is reserved or malformed.")
            if "\0" in value:
                raise ValueError("A build argument value holds a NUL character.")
        for key, value in self.labels.items():
            if _LABEL_KEY.fullmatch(key) is None or "\0" in value:
                raise ValueError("A build label is malformed.")


@dataclass(frozen=True)
class BuildResult:
    """A build that exited cleanly; `log` is scrubbed and holds at most 1 MiB of its tail."""

    vertices: tuple[VertexUpdate, ...]
    log: str = field(repr=False)
    log_truncated: bool = False


def locate_buildx(
    configured: Path | None,
    *,
    search_paths: Sequence[Path] = SEARCH_PATHS,
    path_variable: str | None = None,
) -> BuildxBinary | None:
    """Return the buildx binary, or None when there is none or the one found is not trusted.

    Trust is Compose's: the binary and its directory belong to root or the server's user and
    are writable by nobody else. A configured path that fails is not replaced by a search.
    """
    if configured is not None:
        if usable(configured, event=_UNTRUSTED_EVENT):
            return BuildxBinary((str(configured),))
        logger.warning("buildx_binary_unusable", extra={"path": str(configured)})
        return None
    for candidate in search_paths:
        if usable(candidate, event=_UNTRUSTED_EVENT):
            return BuildxBinary((str(candidate),))
    found = shutil.which(PATH_NAME, path=path_variable)
    if found is not None and usable(Path(found), event=_UNTRUSTED_EVENT):
        return BuildxBinary((found,))
    return None


def build_argv(spec: BuildSpec) -> tuple[str, ...]:
    """Return the arguments after the binary, every value its own element."""
    return (
        "build",
        "--progress",
        "rawjson",
        "--pull=false",
        "--provenance=false",
        "--sbom=false",
        "--tag",
        spec.tag,
        "--file",
        str(spec.context_dir / spec.dockerfile),
        *(("--target", spec.target) if spec.target is not None else ()),
        *(("--no-cache",) if spec.no_cache else ()),
        *(
            part
            for name, value in sorted(spec.args.items())
            for part in ("--build-arg", f"{name}={value}")
        ),
        *(
            part
            for key, value in sorted(spec.labels.items())
            for part in ("--label", f"{key}={value}")
        ),
        str(spec.context_dir),
    )


class BuildxCli:
    """The buildx binary, or the lack of one, the TCP host resolver and the build slots."""

    def __init__(
        self,
        binary: BuildxBinary | None,
        *,
        resolver: Resolver = system_resolver,
        slots: int = MAX_CONCURRENT_BUILDS,
    ) -> None:
        self.binary = binary
        self._resolver = resolver
        self._slots = asyncio.Semaphore(slots)

    @property
    def available(self) -> bool:
        """Report whether a trusted binary was found."""
        return self.binary is not None

    async def build(
        self,
        target: ComposeTarget,
        workdir: Path,
        spec: BuildSpec,
        *,
        scrub: Callable[[str], str] | None = None,
        on_event: Callable[[BuildEvent], None] | None = None,
        deadline: float | None = None,
        limit_seconds: float = BUILD_TIMEOUT_SECONDS,
    ) -> BuildResult:
        """Build one image on the target host from a context inside `workdir`.

        `workdir` is a fresh private directory from `create_invocation_dir` that holds the
        extracted context and nothing named home, config, tmp, tls or buildx; the caller removes
        it, TLS key included, once this returns. `deadline` is an event-loop time past which the
        build gets no longer than a second. At most `slots` builds run at once; the others wait.
        Raises BuildxUnavailable without a binary, ComposeHostRefused for a refused TCP host,
        ValueError for a context outside `workdir`, BuildFailed for a non-zero exit and
        BuildTimedOut for a build killed at its limit.
        """
        if self.binary is None:
            raise BuildxUnavailable
        async with self._slots:
            return await self._build(
                self.binary, target, workdir, spec, scrub, on_event, deadline, limit_seconds
            )

    async def _build(
        self,
        binary: BuildxBinary,
        target: ComposeTarget,
        workdir: Path,
        spec: BuildSpec,
        scrub: Callable[[str], str] | None,
        on_event: Callable[[BuildEvent], None] | None,
        deadline: float | None,
        limit_seconds: float,
    ) -> BuildResult:
        checked = await asyncio.to_thread(_confined, workdir, spec)
        await check_target_host(target, self._resolver)
        target_env = await asyncio.to_thread(_prepare, workdir, target)
        invocation = Invocation(
            argv=build_argv(checked),
            env=_environment(workdir, target_env),
            cwd=checked.context_dir,
            timeout=self._limit(deadline, limit_seconds),
            stdout_cap=0,
            command=("build",),
        )
        sink = _Sink(on_event, scrub)
        started = time.monotonic()
        exit_code, timed_out = await _run(binary, invocation, sink)
        seconds = round(time.monotonic() - started, 3)
        sink.flush()
        log, stderr = await asyncio.to_thread(sink.finish)
        vertices = sink.vertices()
        if timed_out:
            logger.warning("buildx_build_timed_out", extra={"seconds": seconds})
            raise BuildTimedOut(stderr, log, vertices)
        if exit_code != 0:
            logger.warning(
                "buildx_build_failed", extra={"exit_code": exit_code, "seconds": seconds}
            )
            raise BuildFailed(exit_code, stderr, log, vertices)
        logger.debug("buildx_build_finished", extra={"seconds": seconds})
        return BuildResult(vertices, log, sink.log_truncated)

    @staticmethod
    def _limit(deadline: float | None, limit_seconds: float) -> float:
        if deadline is None:
            return limit_seconds
        remaining = deadline - asyncio.get_running_loop().time()
        return min(limit_seconds, max(remaining, MIN_BUILD_SECONDS))


class _Sink:
    """Sorts stderr lines into vertex updates, build output and plain text, all bounded."""

    def __init__(
        self,
        on_event: Callable[[BuildEvent], None] | None,
        scrub: Callable[[str], str] | None,
    ) -> None:
        self.log_truncated = False
        self._on_event = on_event
        self._scrub = scrub
        self._states: dict[str, VertexUpdate] = {}
        self._pending: dict[str, str] = {}
        self._log: deque[str] = deque()
        self._log_bytes = 0
        self._stderr: deque[str] = deque()
        self._stderr_bytes = 0

    def line(self, raw: bytes) -> None:
        document = _status_document(raw)
        if document is None:
            self._text_line(raw)
            return
        for item in _items(document, "vertexes"):
            self._vertex(item)
        for item in _items(document, "logs"):
            self._output(item)

    def vertices(self) -> tuple[VertexUpdate, ...]:
        return tuple(self._scrubbed_vertex(state) for state in self._states.values())

    def flush(self) -> None:
        for digest in list(self._pending):
            self._complete(digest, self._pending.pop(digest))

    def finish(self) -> tuple[str, str]:
        log = _tail(self._scrubbed("\n".join(self._log)), MAX_LOG_BYTES)
        stderr = _tail(self._scrubbed("\n".join(self._stderr)), MAX_STDERR_BYTES)
        return log, stderr

    def _vertex(self, item: Mapping[str, Any]) -> None:
        digest, name = item.get("digest"), item.get("name", "")
        if not (isinstance(digest, str) and isinstance(name, str)):
            return
        if len(digest) > _MAX_DIGEST_CHARS or _DIGEST.fullmatch(digest) is None:
            return
        previous = self._states.get(digest)
        if previous is None and len(self._states) >= MAX_VERTICES:
            return
        error = item.get("error")
        message = error[:MAX_EVENT_TEXT_CHARS] if isinstance(error, str) and error else None
        state = VertexUpdate(
            digest=digest,
            name=(name or (previous.name if previous else ""))[:MAX_EVENT_TEXT_CHARS],
            status=_status_of(item, failed=message is not None),
            cached=item.get("cached") is True,
            error=message,
        )
        if state == previous:
            return
        self._states[digest] = state
        self._emit(self._scrubbed_vertex(state))

    def _output(self, item: Mapping[str, Any]) -> None:
        digest, data = item.get("vertex"), item.get("data")
        if not (isinstance(digest, str) and isinstance(data, str)):
            return
        if digest not in self._states and len(self._pending) >= MAX_VERTICES:
            return
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return
        text = self._pending.pop(digest, "") + chunk.decode("utf-8", errors="replace")
        *lines, rest = _LINE_BREAK.split(text)
        for completed in lines:
            self._complete(digest, completed)
        if len(rest) > MAX_LOG_LINE_CHARS:
            self._complete(digest, rest)
            rest = ""
        if rest:
            self._pending[digest] = rest

    def _complete(self, digest: str, text: str) -> None:
        line = _CONTROL.sub("", text)[:MAX_LOG_LINE_CHARS]
        size = len(line.encode("utf-8")) + 1
        self._log.append(line)
        self._log_bytes += size
        while self._log_bytes > MAX_LOG_BYTES and len(self._log) > 1:
            self._log_bytes -= len(self._log.popleft().encode("utf-8")) + 1
            self.log_truncated = True
        known = self._states.get(digest)
        self._emit(
            LogLine(
                self._scrubbed(known.name) if known is not None else "",
                self._scrubbed(line)[:MAX_EVENT_TEXT_CHARS],
            )
        )

    def _text_line(self, raw: bytes) -> None:
        line = _CONTROL.sub("", raw.decode("utf-8", errors="replace"))[:MAX_EVENT_TEXT_CHARS]
        self._stderr.append(line)
        self._stderr_bytes += len(line.encode("utf-8")) + 1
        while len(self._stderr) > MAX_STDERR_LINES or self._stderr_bytes > MAX_STDERR_BYTES:
            self._stderr_bytes -= len(self._stderr.popleft().encode("utf-8")) + 1

    def _scrubbed(self, text: str) -> str:
        return text if self._scrub is None else self._scrub(text)

    def _scrubbed_vertex(self, state: VertexUpdate) -> VertexUpdate:
        return replace(
            state,
            name=self._scrubbed(state.name),
            error=None if state.error is None else self._scrubbed(state.error),
        )

    def _emit(self, event: BuildEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:
            logger.warning("buildx_event_callback_failed", extra={"error": type(exc).__name__})
            self._on_event = None


async def _run(
    binary: BuildxBinary, invocation: Invocation, sink: _Sink
) -> tuple[int | None, bool]:
    try:
        proc = await spawn(binary, invocation)
    except ComposeUnavailable:
        raise BuildxUnavailable from None
    assert proc.stdout is not None
    assert proc.stderr is not None
    readers = (
        asyncio.ensure_future(pump(proc.stdout, _ignore)),
        asyncio.ensure_future(pump(proc.stderr, sink.line, MAX_PROGRESS_LINE_BYTES)),
    )
    clean = False
    timed_out = False
    try:
        async with asyncio.timeout(invocation.timeout):
            stuck = await drive(proc, readers)
        clean = not stuck
        if stuck:
            logger.warning("buildx_pipe_held_open")
    except TimeoutError:
        timed_out = True
    finally:
        await uninterrupted(release(proc, readers, clean=clean), event="buildx_cleanup_failed")
    return proc.returncode, timed_out


def _ignore(line: bytes) -> None:
    del line


def _is_inner_path(path: str) -> bool:
    parts = path.split("/")
    return (
        path != ""
        and "\0" not in path
        and not path.startswith("/")
        and all(part not in {"", ".."} for part in parts)
    )


def _confined(workdir: Path, spec: BuildSpec) -> BuildSpec:
    root = workdir.resolve(strict=True)
    context = spec.context_dir.resolve(strict=True)
    if context == root or not context.is_relative_to(root) or not context.is_dir():
        raise ValueError("The build context must be a directory inside the work directory.")
    dockerfile = (context / spec.dockerfile).resolve()
    if not dockerfile.is_relative_to(context):
        raise ValueError("The Dockerfile must stay inside the build context.")
    return replace(spec, context_dir=context)


def _prepare(workdir: Path, target: ComposeTarget) -> dict[str, str]:
    populate_invocation_dir(workdir)
    (workdir / BUILDX_CONFIG_DIRECTORY).mkdir(mode=0o700)
    return materialise(target, workdir)


def _environment(workdir: Path, target_env: Mapping[str, str]) -> dict[str, str]:
    environment = build_environment(workdir, target_env, {})
    environment.pop("COMPOSE_MENU", None)
    environment["BUILDX_CONFIG"] = str(workdir / BUILDX_CONFIG_DIRECTORY)
    return environment


def _status_document(raw: bytes) -> dict[str, Any] | None:
    if len(raw) >= MAX_PROGRESS_LINE_BYTES or not raw.lstrip().startswith(b"{"):
        return None
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    present = [key for key in _STATUS_KEYS if key in document]
    if not present or any(
        document[key] is not None and not isinstance(document[key], list) for key in present
    ):
        return None
    return document


def _items(document: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = document.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _status_of(item: Mapping[str, Any], *, failed: bool) -> VertexState:
    if failed:
        return "error"
    if isinstance(item.get("completed"), str):
        return "done"
    if isinstance(item.get("started"), str):
        return "running"
    return "waiting"


def _tail(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[-limit:].decode("utf-8", errors="ignore")


def _reject_constant(constant: str) -> Any:
    raise ValueError(constant)
