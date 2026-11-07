"""Runs Compose commands for one project: argv construction, process slots and error mapping."""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.environment import build_environment
from app.compose_cli.errors import ComposeBusy, ComposeCommandFailed, ComposeTimedOut
from app.compose_cli.host_check import check_target_host
from app.compose_cli.parsers import (
    ComposeVersion,
    ProgressEvent,
    PsEntry,
    ResolvedProject,
    parse_config,
    parse_ps,
    parse_version,
)
from app.compose_cli.process import Completed, Invocation, run, uninterrupted
from app.compose_cli.sources import (
    SOURCES_FILE,
    BuiltImages,
    ComposeSource,
    number_sources,
    override_document,
)
from app.compose_cli.target import ComposeTarget, materialise
from app.compose_cli.workdir import (
    EMPTY_ENV_FILE,
    create_invocation_dir,
    populate_invocation_dir,
    remove_invocation_dir,
    write_private,
    write_sources_override,
)
from app.logging import Scrubber, get_logger
from app.netcheck import Resolver, system_resolver

logger = get_logger(__name__)

MUTATION_SLOT_WAIT_SECONDS: Final = 30.0
MIN_MUTATION_SECONDS: Final = 1.0
VERSION_TIMEOUT_SECONDS: Final = 10.0
CONFIG_TIMEOUT_SECONDS: Final = 30.0
PS_TIMEOUT_SECONDS: Final = 30.0
MAX_STDOUT_BYTES: Final = 8 * 1024 * 1024
DATA_PLACEHOLDER: Final = "<data>"

VERSION_TAIL: Final = ("version", "--format", "json")
CONFIG_TAIL: Final = ("config", "--format", "json")
MERGE_TAIL: Final = ("config", "--no-interpolate", "--no-path-resolution", "--format", "yaml")
PS_TAIL: Final = ("ps", "--all", "--format", "json")
START_TAIL: Final = ("start",)
STOP_TAIL: Final = ("stop",)

MAX_NAMED_SERVICES: Final = 256

_PROJECT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_SERVICE_NAME: Final = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class ComposeProject:
    """A Compose project: its name, its directory and the file inside it.

    `extra_files` are further Compose files merged after `file`. `sources` are delivered to
    Compose as environment variables and `sensitive` are strings to redact from its output;
    neither takes part in comparison or in a repr. `built` pairs each service the product built
    with its image, which replaces the service's `build`.
    """

    name: str
    directory: Path
    file: Path
    extra_files: tuple[Path, ...] = ()
    sources: tuple[ComposeSource, ...] = field(default=(), repr=False, compare=False)
    sensitive: frozenset[str] = field(default=frozenset(), repr=False, compare=False)
    built: BuiltImages = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if _PROJECT_NAME.fullmatch(self.name) is None:
            raise ValueError("Compose project name is outside the permitted grammar.")
        if not (
            self.directory.is_absolute()
            and self.file.is_absolute()
            and all(item.is_absolute() for item in self.extra_files)
        ):
            raise ValueError("Compose project paths must be absolute.")


def is_service_name(name: str) -> bool:
    """Report whether the text can be passed to Compose as a service name, never as a flag."""
    return _SERVICE_NAME.fullmatch(name) is not None


def service_tail(tail: tuple[str, ...], services: Sequence[str]) -> tuple[str, ...]:
    """Return the command with the named services after it, each checked and listed once."""
    if len(services) > MAX_NAMED_SERVICES:
        raise ValueError("Too many Compose services are named.")
    if not all(is_service_name(name) for name in services):
        raise ValueError("Compose service name is outside the permitted grammar.")
    return (*tail, *dict.fromkeys(services))


def up_tail(*, remove_orphans: bool) -> tuple[str, ...]:
    """Return the `up` command and flags."""
    tail = ("up", "--detach", "--pull", "never", "--no-build")
    return (*tail, "--remove-orphans") if remove_orphans else tail


def down_tail(*, remove_orphans: bool) -> tuple[str, ...]:
    """Return the `down` command and flags; volumes and images are never removed."""
    return ("down", "--remove-orphans") if remove_orphans else ("down",)


def build_argv(
    project: ComposeProject,
    env_file: Path,
    tail: tuple[str, ...],
    *,
    all_profiles: bool = False,
) -> tuple[str, ...]:
    """Return the argument list after the binary, every value its own element."""
    return (
        "--progress",
        "json",
        "--ansi",
        "never",
        "--project-name",
        project.name,
        "--project-directory",
        str(project.directory),
        "--file",
        str(project.file),
        *(part for item in project.extra_files for part in ("--file", str(item))),
        "--env-file",
        str(env_file),
        *(("--profile", "*") if all_profiles else ()),
        *tail,
    )


class ComposeCli:
    """The Compose binary together with the process-wide slot limit and work directory base."""

    def __init__(
        self,
        binary: ComposeBinary,
        *,
        work_base: Path,
        slots: int = 4,
        operation_timeout: float = 600.0,
        resolver: Resolver = system_resolver,
    ) -> None:
        self.binary = binary
        self.operation_timeout = operation_timeout
        self._resolver = resolver
        self._work_base = work_base
        self._slots = asyncio.Semaphore(slots)
        self._data_dir_pattern = _data_dir_pattern(work_base.parent)

    @property
    def work_base(self) -> Path:
        """Return the private directory the per-invocation work directories are made in."""
        return self._work_base

    async def version(self) -> ComposeVersion:
        """Run `version --format json` and parse it."""
        async with self._workdir(None) as (workdir, _):
            invocation = Invocation(
                argv=VERSION_TAIL,
                env=build_environment(workdir, {}, {}),
                cwd=workdir,
                timeout=VERSION_TIMEOUT_SECONDS,
                stdout_cap=MAX_STDOUT_BYTES,
                command=VERSION_TAIL,
            )
            async with self.slot(None):
                completed = await run(self.binary, invocation, scrub=self.strip_data_dir)
        if completed.exit_code != 0:
            raise ComposeCommandFailed(
                VERSION_TAIL, completed.exit_code, completed.stderr, completed.events
            )
        return parse_version(completed.stdout)

    async def merged_config(self, name: str, texts: Sequence[str]) -> str:
        """Merge Compose files with `config` and return the YAML, without interpolating variables.

        The files are written to a private work directory that holds nothing else, so a reference
        they make to another file finds none. Their text must already have passed the pre-check.
        """
        async with self._workdir(None) as (workdir, _):
            paths = [workdir / f"merge-{number}.yaml" for number in range(len(texts))]
            for path, text in zip(paths, texts, strict=True):
                await asyncio.to_thread(write_private, path, text.encode("utf-8"))
            project = ComposeProject(name, workdir, paths[0], tuple(paths[1:]))
            invocation = Invocation(
                argv=build_argv(project, workdir / EMPTY_ENV_FILE, MERGE_TAIL),
                env=build_environment(workdir, {}, {}),
                cwd=workdir,
                timeout=CONFIG_TIMEOUT_SECONDS,
                stdout_cap=MAX_STDOUT_BYTES,
                command=MERGE_TAIL,
            )
            async with self.slot(None):
                completed = await run(self.binary, invocation, scrub=self.strip_data_dir)
        if completed.exit_code != 0:
            raise ComposeCommandFailed(
                MERGE_TAIL, completed.exit_code, completed.stderr, completed.events
            )
        return completed.stdout.decode("utf-8", errors="replace")

    @asynccontextmanager
    async def invocation(
        self,
        project: ComposeProject,
        target: ComposeTarget,
        variables: Mapping[str, str],
        scrubber: Scrubber,
        *,
        deadline: float | None = None,
    ) -> AsyncIterator["ComposeRun"]:
        """Create the work directory and environment for a run, and remove them on exit.

        Enter and leave inside the task that runs the commands; the TLS key is deleted on exit.
        `deadline` is an event-loop time after which no mutating command gets longer to run.
        Raises ComposeHostRefused before anything is created when a TCP target's host is on an
        address the address policy refuses.
        """
        await check_target_host(target, self._resolver)
        numbered = number_sources(project.sources)
        document = override_document(numbered, project.built) if numbered or project.built else None
        async with self._workdir(target, document) as (workdir, target_env):
            environment = build_environment(
                workdir,
                target_env,
                variables,
                {name: item.content for name, item in numbered},
            )
            for value in project.sensitive:
                scrubber.register_secret(value)
            effective = (
                replace(project, extra_files=(*project.extra_files, workdir / SOURCES_FILE))
                if document is not None
                else project
            )
            yield ComposeRun(self, effective, workdir, environment, scrubber, deadline)

    def strip_data_dir(self, text: str) -> str:
        """Replace the data directory path with a placeholder."""
        return self._data_dir_pattern.sub(DATA_PLACEHOLDER, text)

    @asynccontextmanager
    async def slot(self, wait: float | None) -> AsyncIterator[None]:
        """Hold one of the process slots; raise ComposeBusy when wait seconds pass without one."""
        if wait is None:
            await self._slots.acquire()
        else:
            try:
                async with asyncio.timeout(wait):
                    await self._slots.acquire()
            except TimeoutError:
                raise ComposeBusy from None
        try:
            yield
        finally:
            self._slots.release()

    @asynccontextmanager
    async def _workdir(
        self, target: ComposeTarget | None, override: str | None = None
    ) -> AsyncIterator[tuple[Path, Mapping[str, str]]]:
        task = asyncio.ensure_future(asyncio.to_thread(_prepare, self._work_base, target, override))
        try:
            workdir, target_env = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_discard_prepared)
            raise
        try:
            yield workdir, target_env
        finally:
            await uninterrupted(asyncio.to_thread(remove_invocation_dir, workdir))


class ComposeRun:
    """Commands against one project inside one invocation's work directory and environment."""

    def __init__(
        self,
        cli: ComposeCli,
        project: ComposeProject,
        workdir: Path,
        environment: Mapping[str, str],
        scrubber: Scrubber,
        deadline: float | None = None,
    ) -> None:
        self._cli = cli
        self._project = project
        self._workdir = workdir
        self._environment = environment
        self._scrubber = scrubber
        self._deadline = deadline

    async def config(self, *, slot_wait: float | None = None) -> ResolvedProject:
        """Resolve the project with every profile enabled."""
        completed = await self._read(
            CONFIG_TAIL, CONFIG_TIMEOUT_SECONDS, all_profiles=True, slot_wait=slot_wait
        )
        return await asyncio.to_thread(parse_config, completed.stdout)

    async def ps(self) -> tuple[PsEntry, ...]:
        """List the project's containers, running or not."""
        completed = await self._read(PS_TAIL, PS_TIMEOUT_SECONDS)
        return await asyncio.to_thread(parse_ps, completed.stdout)

    async def compose_up(
        self,
        *,
        remove_orphans: bool,
        on_event: Callable[[ProgressEvent], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> Completed:
        """Create and start the project's services without pulling or building.

        `on_start` runs once a process slot is held, immediately before the process starts.
        """
        return await self._mutate(up_tail(remove_orphans=remove_orphans), on_event, on_start)

    async def compose_down(
        self,
        *,
        remove_orphans: bool,
        on_event: Callable[[ProgressEvent], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> Completed:
        """Stop and remove the project's containers and networks, keeping volumes and images."""
        return await self._mutate(down_tail(remove_orphans=remove_orphans), on_event, on_start)

    async def compose_start(
        self,
        *,
        services: Sequence[str] = (),
        on_event: Callable[[ProgressEvent], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> Completed:
        """Start the project's existing containers, or only the named services.

        Compose also starts a stopped service that a named one depends on.
        """
        return await self._mutate(service_tail(START_TAIL, services), on_event, on_start)

    async def compose_stop(
        self,
        *,
        services: Sequence[str] = (),
        on_event: Callable[[ProgressEvent], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> Completed:
        """Stop the project's running containers, or only the named services."""
        return await self._mutate(service_tail(STOP_TAIL, services), on_event, on_start)

    async def _mutate(
        self,
        tail: tuple[str, ...],
        on_event: Callable[[ProgressEvent], None] | None,
        on_start: Callable[[], None] | None,
    ) -> Completed:
        limit = self._mutation_limit()
        return await self._execute(
            tail,
            limit_seconds=limit,
            stdout_events=True,
            all_profiles=False,
            slot_wait=min(MUTATION_SLOT_WAIT_SECONDS, limit),
            on_event=on_event,
            on_start=on_start,
        )

    def _mutation_limit(self) -> float:
        limit = self._cli.operation_timeout
        if self._deadline is None:
            return limit
        remaining = self._deadline - asyncio.get_running_loop().time()
        return min(limit, max(remaining, MIN_MUTATION_SECONDS))

    async def _read(
        self,
        tail: tuple[str, ...],
        limit_seconds: float,
        *,
        all_profiles: bool = False,
        slot_wait: float | None = None,
    ) -> Completed:
        return await self._execute(
            tail,
            limit_seconds=limit_seconds,
            stdout_events=False,
            all_profiles=all_profiles,
            slot_wait=slot_wait,
            on_event=None,
            on_start=None,
        )

    async def _execute(
        self,
        tail: tuple[str, ...],
        *,
        limit_seconds: float,
        stdout_events: bool,
        all_profiles: bool,
        slot_wait: float | None,
        on_event: Callable[[ProgressEvent], None] | None,
        on_start: Callable[[], None] | None,
    ) -> Completed:
        invocation = Invocation(
            argv=build_argv(
                self._project,
                self._workdir / EMPTY_ENV_FILE,
                tail,
                all_profiles=all_profiles,
            ),
            env=self._environment,
            cwd=self._project.directory,
            timeout=limit_seconds,
            stdout_cap=MAX_STDOUT_BYTES,
            command=tail,
            stdout_events=stdout_events,
        )
        command = " ".join(tail)
        started = time.monotonic()
        try:
            async with self._cli.slot(slot_wait):
                if on_start is not None:
                    on_start()
                completed = await run(
                    self._cli.binary, invocation, on_event=on_event, scrub=self._scrub
                )
        except ComposeTimedOut:
            logger.warning("compose_command_timed_out", extra={"command": command})
            raise
        elapsed = round(time.monotonic() - started, 3)
        if completed.exit_code != 0:
            logger.warning(
                "compose_command_failed",
                extra={"command": command, "exit_code": completed.exit_code, "seconds": elapsed},
            )
            raise ComposeCommandFailed(
                tail, completed.exit_code, completed.stderr, completed.events
            )
        logger.debug("compose_command_finished", extra={"command": command, "seconds": elapsed})
        return completed

    def _scrub(self, text: str) -> str:
        return self._scrubber.scrub_text(self._cli.strip_data_dir(text))


def _prepare(
    base: Path, target: ComposeTarget | None, override: str | None
) -> tuple[Path, dict[str, str]]:
    workdir = create_invocation_dir(base)
    try:
        populate_invocation_dir(workdir)
        if override is not None:
            write_sources_override(workdir, override, SOURCES_FILE)
        target_env = materialise(target, workdir) if target is not None else {}
    except BaseException:
        remove_invocation_dir(workdir)
        raise
    return workdir, target_env


def _discard_prepared(task: "asyncio.Future[tuple[Path, dict[str, str]]]") -> None:
    if not task.cancelled() and task.exception() is None:
        asyncio.get_running_loop().run_in_executor(None, remove_invocation_dir, task.result()[0])


def _data_dir_pattern(data_dir: Path) -> re.Pattern[str]:
    spellings = {str(data_dir), str(data_dir.resolve())}
    alternatives = "|".join(
        re.escape(s) for s in sorted(spellings, key=len, reverse=True) if len(s) > 1
    )
    if not alternatives:
        return re.compile(r"(?!)")
    return re.compile(rf"(?:{alternatives})(?![\w.\-])")
