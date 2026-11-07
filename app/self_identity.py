"""Which container dockmin runs in: read from /proc and verified against the environments."""

import asyncio
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal, Protocol

from starlette.applications import Starlette

from app.db.repositories.environment import ConnectionKind
from app.logging import get_logger

logger = get_logger(__name__)

MOUNTINFO_FILE: Final = Path("/proc/self/mountinfo")
CGROUP_FILE: Final = Path("/proc/self/cgroup")
PROBE_TIMEOUT_SECONDS: Final = 8.0
RETRY_INITIAL_SECONDS: Final = 5.0
RETRY_CEILING_SECONDS: Final = 300.0
HOST_FILES: Final = frozenset({"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"})

_CONTAINER_ROOT = re.compile(r"/containers/([0-9a-f]{64})/")
_CGROUP_ID = re.compile(r"(?:^|[/-])([0-9a-f]{64})(?:\.scope)?(?=/|$)")
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")

type IdentitySource = Literal["mountinfo", "cgroup"]


@dataclass(frozen=True, slots=True)
class Candidate:
    """A container id found in /proc and the file it came from."""

    container_id: str
    source: IdentitySource


@dataclass(frozen=True, slots=True)
class HomeEnvironment:
    """An environment whose Engine reports dockmin's own container."""

    environment_id: int
    connection_kind: ConnectionKind
    container_name: str


@dataclass(frozen=True, slots=True)
class SelfIdentity:
    """The container dockmin runs in, with what it owns in each environment that holds it."""

    container_id: str | None = None
    source: IdentitySource | None = None
    verified: tuple[HomeEnvironment, ...] = ()
    volumes: Mapping[int, frozenset[str]] = field(default_factory=dict)
    compose_project: Mapping[int, str] = field(default_factory=dict)
    swarm_service: Mapping[int, str] = field(default_factory=dict)
    swarm_node: Mapping[int, str] = field(default_factory=dict)
    restartable_via: int | None = None
    unreadable: tuple[int, ...] = ()
    listing_failed: bool = False
    mount_sources: Mapping[int, frozenset[str]] = field(default_factory=dict)

    @property
    def containerised(self) -> bool:
        """Report whether a container of its own was found."""
        return self.container_id is not None

    @property
    def incomplete(self) -> bool:
        """Report whether an environment could not be read, or the list not fetched, last time."""
        return self.listing_failed or bool(self.unreadable)

    def is_home(self, environment_id: int) -> bool:
        """Report whether the environment's Engine reports dockmin's own container."""
        return any(home.environment_id == environment_id for home in self.verified)

    def protects_container(self, container_id: str) -> bool:
        """Report whether the id is dockmin's own container, in any environment."""
        return self.container_id is not None and container_id == self.container_id

    def protects_volume(self, environment_id: int, name: str) -> bool:
        """Report whether the named volume is mounted in dockmin's container in this environment."""
        return name in self.volumes.get(environment_id, frozenset())

    def mounts_unknown(self, environment_id: int) -> bool:
        """Report whether dockmin's own mounts on this environment's host cannot be established.

        True for a containerised process whose last resolution could not read the environment or
        list the environments, and for a home environment that recorded no mount list.
        """
        if self.container_id is None:
            return False
        if self.listing_failed or environment_id in self.unreadable:
            return True
        return self.is_home(environment_id) and environment_id not in self.mount_sources

    def protects_path(self, environment_id: int, path: str) -> bool:
        """Report whether the path holds, lies inside, or is a source of dockmin's mounts.

        True for every path while `mounts_unknown`.
        """
        if self.mounts_unknown(environment_id):
            return True
        return any(
            source == path
            or source == "/"
            or path.startswith(f"{source}/")
            or source.startswith(f"{path}/")
            for source in self.mount_sources.get(environment_id, frozenset())
        )

    def protects_stack(self, environment_id: int, name: str) -> bool:
        """Report whether the Compose project is the one dockmin's container belongs to."""
        project = self.compose_project.get(environment_id)
        return project is not None and project == name.casefold()

    def protects_service(self, environment_id: int, service_id: str) -> bool:
        """Report whether the Swarm service is the one running dockmin's container."""
        return self.swarm_service.get(environment_id) == service_id

    def protects_node(self, environment_id: int, node_id: str) -> bool:
        """Report whether the Swarm node is the one running dockmin's container."""
        return self.swarm_node.get(environment_id) == node_id


NOT_CONTAINERISED: Final = SelfIdentity()

type SelfIdentitySource = Callable[[], SelfIdentity]


def no_identity() -> SelfIdentity:
    """Return the identity of a process that found no container of its own."""
    return NOT_CONTAINERISED


def source_of(app: Starlette) -> SelfIdentitySource:
    """Return a reader of the identity the application currently holds."""

    def current() -> SelfIdentity:
        held = getattr(app.state, "self_identity", None)
        return held if isinstance(held, SelfIdentity) else NOT_CONTAINERISED

    return current


def _unescape(field_text: str) -> str:
    return _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match[1], 8)), field_text)


def container_id_from_mountinfo(text: str) -> str | None:
    """Return the container id the mounts of the host files agree on, else None.

    Docker bind-mounts /etc/hostname, /etc/hosts and /etc/resolv.conf from a directory named for
    the container, so the mount roots carry the id even in a private cgroup namespace.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        mount_point = _unescape(fields[4])
        if mount_point not in HOST_FILES:
            continue
        match = _CONTAINER_ROOT.search(_unescape(fields[3]))
        if match is None:
            found.pop(mount_point, None)
        else:
            found[mount_point] = match[1]
    ids = set(found.values())
    return next(iter(ids)) if len(ids) == 1 else None


def container_id_from_cgroup(text: str) -> str | None:
    """Return the single 64-hex id named by the cgroup paths, else None."""
    ids: set[str] = set()
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            ids.update(_CGROUP_ID.findall(parts[2]))
    return next(iter(ids)) if len(ids) == 1 else None


def find_candidate(mountinfo: str, cgroup: str) -> Candidate | None:
    """Return the container id from the mounts, else from the cgroup, else None."""
    from_mounts = container_id_from_mountinfo(mountinfo)
    if from_mounts is not None:
        return Candidate(from_mounts, "mountinfo")
    from_cgroup = container_id_from_cgroup(cgroup)
    if from_cgroup is not None:
        return Candidate(from_cgroup, "cgroup")
    return None


@dataclass(frozen=True, slots=True)
class EnvironmentRef:
    """An environment to ask about the candidate."""

    environment_id: int
    connection_kind: ConnectionKind
    is_swarm: bool = False


@dataclass(frozen=True, slots=True)
class ContainerFacts:
    """What verification and protection need about one container.

    `hostname` is None when the container was matched by its exact id on a Swarm task, which
    carries no hostname.
    """

    container_id: str
    name: str
    hostname: str | None
    volumes: frozenset[str] = frozenset()
    compose_project: str | None = None
    swarm_service_id: str | None = None
    swarm_node_id: str | None = None
    mount_sources: frozenset[str] | None = None


class HostProbe(Protocol):
    """Reads environments and containers for one resolution."""

    async def environments(self) -> Sequence[EnvironmentRef]:
        """Return every environment."""
        ...

    async def inspect(self, ref: EnvironmentRef, container_id: str) -> ContainerFacts | None:
        """Return the container, or None when the environment holds none with exactly this id.

        Raises when the environment cannot be read.
        """
        ...


type Reader = Callable[[Path], Awaitable[str]]


async def read_proc(path: Path) -> str:
    """Return a /proc file's text, or an empty string when it cannot be read."""

    def read() -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    return await asyncio.to_thread(read)


@dataclass(frozen=True, slots=True)
class _Home:
    ref: EnvironmentRef
    facts: ContainerFacts


class SelfIdentityResolver:
    """Resolves the identity, and re-resolves it in the background without blocking callers."""

    def __init__(
        self,
        *,
        probe: HostProbe,
        publish: Callable[[SelfIdentity], None],
        read: Reader = read_proc,
        hostname: Callable[[], str] = socket.gethostname,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_initial: float = RETRY_INITIAL_SECONDS,
        retry_ceiling: float = RETRY_CEILING_SECONDS,
    ) -> None:
        self._probe = probe
        self._publish = publish
        self._read = read
        self._hostname = hostname
        self._timeout = timeout
        self._sleep = sleep
        self._retry_initial = retry_initial
        self._retry_ceiling = retry_ceiling
        self._current = NOT_CONTAINERISED
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._again = False
        self._wake = asyncio.Event()
        self._closed = False

    @property
    def current(self) -> SelfIdentity:
        """Return the last identity published."""
        return self._current

    async def resolve(self) -> SelfIdentity:
        """Read /proc, verify the candidate against every environment, publish and return it."""
        async with self._lock:
            mountinfo, cgroup = await asyncio.gather(
                self._read(MOUNTINFO_FILE), self._read(CGROUP_FILE)
            )
            candidate = find_candidate(mountinfo, cgroup)
            identity = NOT_CONTAINERISED if candidate is None else await self._verified(candidate)
            self._current = identity
            self._publish(identity)
            return identity

    def request(self) -> None:
        """Ask for a re-resolution soon; calls made while one runs are folded into one more."""
        if self._closed:
            return
        if self._task is not None and not self._task.done():
            self._again = True
            self._wake.set()
            return
        self._start(attempt_first=True)

    def watch(self) -> None:
        """Retry with backoff while the current identity is incomplete; otherwise do nothing."""
        if self._closed or not self._current.incomplete:
            return
        if self._task is not None and not self._task.done():
            return
        self._start(attempt_first=False)

    async def aclose(self) -> None:
        """Cancel a re-resolution in flight and refuse further requests."""
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _start(self, *, attempt_first: bool) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._repeat(attempt_first=attempt_first))

    async def _repeat(self, *, attempt_first: bool) -> None:
        delay = self._retry_initial
        skip = not attempt_first
        incomplete = True
        while True:
            if not skip:
                self._again = False
                incomplete = await self._attempt()
                if self._again:
                    delay = self._retry_initial
                    continue
            skip = False
            if not incomplete:
                return
            if await self._pause(delay):
                delay = self._retry_initial
            else:
                delay = min(delay * 2, self._retry_ceiling)

    async def _attempt(self) -> bool:
        try:
            return (await self.resolve()).incomplete
        except Exception as error:
            logger.warning("self_identity_resolution_failed", extra={"error": type(error).__name__})
            return True

    async def _pause(self, seconds: float) -> bool:
        """Wait out the delay; True when a request ended the wait early."""
        self._wake.clear()
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            done, _ = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waker.cancel()
            await asyncio.gather(sleeper, waker, return_exceptions=True)
        return waker in done

    async def _verified(self, candidate: Candidate) -> SelfIdentity:
        previous = self._current if self._current.container_id == candidate.container_id else None
        try:
            refs = await self._probe.environments()
            outcomes = await asyncio.gather(
                *(self._ask(ref, candidate.container_id) for ref in refs)
            )
        except Exception as error:
            logger.warning("self_identity_probe_failed", extra={"error": type(error).__name__})
            if previous is not None:
                return replace(previous, listing_failed=True)
            return SelfIdentity(candidate.container_id, candidate.source, listing_failed=True)
        homes: list[_Home] = []
        unreadable: list[int] = []
        for ref, outcome in zip(refs, outcomes, strict=True):
            if isinstance(outcome, ContainerFacts):
                homes.append(_Home(ref, outcome))
            elif outcome == "unreadable":
                kept = None if previous is None else _retained(previous, ref)
                if kept is not None:
                    homes.append(kept)
                else:
                    unreadable.append(ref.environment_id)
        return _identity_of(candidate, homes, unreadable)

    async def _ask(
        self, ref: EnvironmentRef, container_id: str
    ) -> ContainerFacts | Literal["absent", "unreadable"]:
        try:
            async with asyncio.timeout(self._timeout):
                facts = await self._probe.inspect(ref, container_id)
        except Exception as error:
            logger.debug(
                "self_identity_environment_unreadable",
                extra={"environment_id": ref.environment_id, "error": type(error).__name__},
            )
            return "unreadable"
        if facts is None or facts.container_id != container_id:
            return "absent"
        if facts.hostname is not None and facts.hostname != self._hostname():
            return "absent"
        return facts


def _retained(previous: SelfIdentity, ref: EnvironmentRef) -> _Home | None:
    environment_id = ref.environment_id
    home = next((h for h in previous.verified if h.environment_id == environment_id), None)
    if home is None or previous.container_id is None:
        return None
    return _Home(
        ref,
        ContainerFacts(
            previous.container_id,
            home.container_name,
            None,
            previous.volumes.get(environment_id, frozenset()),
            previous.compose_project.get(environment_id),
            previous.swarm_service.get(environment_id),
            previous.swarm_node.get(environment_id),
            previous.mount_sources.get(environment_id),
        ),
    )


def _identity_of(
    candidate: Candidate, homes: Sequence[_Home], unreadable: Sequence[int] = ()
) -> SelfIdentity:
    ordered = sorted(
        homes, key=lambda h: (h.ref.connection_kind != "unix_socket", h.ref.environment_id)
    )
    local = next((h for h in ordered if h.ref.connection_kind == "unix_socket"), None)
    return SelfIdentity(
        container_id=candidate.container_id,
        source=candidate.source,
        verified=tuple(
            HomeEnvironment(h.ref.environment_id, h.ref.connection_kind, h.facts.name)
            for h in ordered
        ),
        volumes={h.ref.environment_id: h.facts.volumes for h in ordered if h.facts.volumes},
        compose_project={
            h.ref.environment_id: h.facts.compose_project.casefold()
            for h in ordered
            if h.facts.compose_project
        },
        swarm_service={
            h.ref.environment_id: h.facts.swarm_service_id
            for h in ordered
            if h.facts.swarm_service_id
        },
        swarm_node={
            h.ref.environment_id: h.facts.swarm_node_id for h in ordered if h.facts.swarm_node_id
        },
        restartable_via=None if local is None else local.ref.environment_id,
        unreadable=tuple(sorted(unreadable)),
        mount_sources={
            h.ref.environment_id: h.facts.mount_sources
            for h in ordered
            if h.facts.mount_sources is not None
        },
    )
