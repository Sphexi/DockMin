"""Reads a host directory or a volume through a created, never-started helper container."""

import asyncio
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app.auth.principal import Principal
from app.docker_engine.archive import ArchiveError, DirectoryStream
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.logging import get_logger
from app.services.guards import require_administrator

logger = get_logger(__name__)

MOUNT_POINT: Final = "/dockmin-read"
HELPER_NAME_PREFIX: Final = "dockmin-read-"
HELPER_ROLE_LABEL: Final = "dockmin.role"
HELPER_ROLE: Final = "file-reader"
LEFTOVER_AGE_SECONDS: Final = 600
REMOVE_SECONDS: Final = 30.0
PATH_MAX_BYTES: Final = 1024
MAX_IMAGE_INSPECTS: Final = 16

_HELPER_NAME = re.compile(r"dockmin-read-[0-9a-f]{12}")
_CREATED_STATE: Final = "created"
_NAME_RANDOM_BYTES: Final = 6
_BAD_REQUEST: Final = 400
_IN_USE: set[str] = set()


class WorkingDirectoryProblem(StrEnum):
    """Why a working directory cannot be bind-mounted."""

    NOT_ABSOLUTE = "not_absolute"
    ROOT = "root"
    NOT_NORMALISED = "not_normalised"
    CONTROL_CHARACTERS = "control_characters"
    BIND_SEPARATOR = "bind_separator"
    TOO_LONG = "too_long"


class InvalidWorkingDirectory(ValueError):
    """The working directory failed `check_working_directory`."""

    def __init__(self, problem: WorkingDirectoryProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


class HelperMountRefused(Exception):
    """The Engine refused to create the helper over the working directory."""


class HelperImageMissing(Exception):
    """The host holds no image a helper can be created from."""

    reason: Final = "helper_image_missing"


class FileRefusalReason(StrEnum):
    """Why one path was not returned; the values of the archive errors' `code` are included."""

    MISSING = "missing"
    SYMLINK = "symlink"
    NOT_REGULAR_FILE = "not_regular_file"
    TOO_LARGE = "too_large"
    MALFORMED = "malformed"
    MEMBER_COUNT = "member_count"
    MEMBER_NAME = "member_name"
    INVALID_PATH = "invalid_path"


@dataclass(frozen=True, slots=True)
class HostFile:
    """A file read from the host; `relative_path` is the path as requested."""

    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class FileRefusal:
    """A path that was not read; nothing of the file is carried."""

    relative_path: str
    reason: FileRefusalReason


type FileRead = HostFile | FileRefusal


@dataclass(frozen=True, slots=True)
class HelperLeftover:
    """A helper container found carrying the helper label before a read."""

    container_id: str
    state: str
    removed: bool


@dataclass(frozen=True, slots=True)
class HostFilesResult:
    """The outcome of `HostFileReader.read`, keyed by requested path."""

    helper_container: str
    results: Mapping[str, FileRead]
    leftovers: tuple[HelperLeftover, ...]
    helper_removed: bool

    @property
    def refused(self) -> dict[str, FileRefusalReason]:
        """Return the reason for every path that was refused."""
        return {
            path: result.reason
            for path, result in self.results.items()
            if isinstance(result, FileRefusal)
        }


def check_working_directory(path: str) -> str:
    """Return `path` unchanged, or raise InvalidWorkingDirectory.

    The path must be absolute, normalised, not `/`, free of control characters and `:` (the
    separator of a bind), and at most 1,024 bytes.
    """
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.CONTROL_CHARACTERS) from None
    if size > PATH_MAX_BYTES:
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.TOO_LONG)
    if not path.startswith("/"):
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.NOT_ABSOLUTE)
    if _has_control_character(path):
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.CONTROL_CHARACTERS)
    if ":" in path:
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.BIND_SEPARATOR)
    if path == "/":
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.ROOT)
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        raise InvalidWorkingDirectory(WorkingDirectoryProblem.NOT_NORMALISED)
    return path


def is_relative_path(path: str) -> bool:
    """Report whether `path` is a normalised path below the working directory."""
    try:
        size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (
        0 < size <= PATH_MAX_BYTES
        and not path.startswith("/")
        and not _has_control_character(path)
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _has_control_character(text: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def helper_body(image: str, working_dir: str) -> dict[str, Any]:
    """Build the create body of a helper: no network, read-only root, no capabilities.

    The directory is a `Mounts` bind, which the Engine refuses when the source is missing; a
    `Binds` entry would have created it on the host.
    """
    return {
        "Image": image,
        "Labels": {HELPER_ROLE_LABEL: HELPER_ROLE},
        "NetworkDisabled": True,
        "HostConfig": {
            "Mounts": [
                {"Type": "bind", "Source": working_dir, "Target": MOUNT_POINT, "ReadOnly": True}
            ],
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "AutoRemove": False,
        },
    }


def volume_helper_body(image: str, volume: str) -> dict[str, Any]:
    """Build the create body of a volume helper: the volume read-only, nothing else reachable."""
    return {
        "Image": image,
        "Labels": {HELPER_ROLE_LABEL: HELPER_ROLE},
        "NetworkDisabled": True,
        "HostConfig": {
            "Mounts": [
                {"Type": "volume", "Source": volume, "Target": MOUNT_POINT, "ReadOnly": True}
            ],
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "AutoRemove": False,
        },
    }


async def helper_image(client: EngineClient) -> str:
    """Return the id of an image on the host to create a helper from.

    Smaller images are inspected first, and the first that declares no volume wins, since a
    declared volume is created anonymously with the helper. With every candidate declaring one
    the smallest is used. HelperImageMissing when the host has no image.
    """
    images = sorted(await client.images.list(), key=lambda image: (image.size, image.id))
    if not images:
        raise HelperImageMissing
    for image in images[:MAX_IMAGE_INSPECTS]:
        try:
            detail = await client.images.inspect(image.id)
        except NotFound:
            continue
        if not detail.config.get("Volumes"):
            return image.id
    return images[0].id


class HostFileSession:
    """Reads files through one helper container; valid only inside `HostFileReader.session`."""

    def __init__(
        self, client: EngineClient, container_id: str, leftovers: tuple[HelperLeftover, ...]
    ) -> None:
        self._client = client
        self.container_id = container_id
        self.leftovers = leftovers
        self.removed = False
        self._open = True

    async def read(self, relative_path: str, *, limit: int) -> FileRead:
        """Read one file below the working directory, or say why it was refused.

        Engine failures other than a refusal of the file itself propagate.
        """
        if not self._open:
            raise RuntimeError("the helper container has been removed")
        if not is_relative_path(relative_path):
            return FileRefusal(relative_path, FileRefusalReason.INVALID_PATH)
        try:
            entry = await self._client.containers.archive(
                self.container_id, f"{MOUNT_POINT}/{relative_path}", limit=limit
            )
        except ArchiveError as error:
            return FileRefusal(relative_path, FileRefusalReason(error.code))
        return HostFile(relative_path, entry.content)

    async def read_all(self, relative_paths: Sequence[str], *, limit: int) -> dict[str, FileRead]:
        """Read each path in turn, keyed by path; a repeated path is read once."""
        return {path: await self.read(path, limit=limit) for path in dict.fromkeys(relative_paths)}

    def close(self) -> None:
        """Refuse further reads."""
        self._open = False


class VolumeHelper:
    """A helper container with one volume mounted read-only; valid only inside `volume_session`."""

    def __init__(
        self, client: EngineClient, container_id: str, leftovers: tuple[HelperLeftover, ...]
    ) -> None:
        self._client = client
        self.container_id = container_id
        self.leftovers = leftovers
        self.removed = False

    async def stream(self) -> DirectoryStream:
        """Stream the volume's contents as a tar; the caller closes the stream."""
        return await self._client.containers.archive_directory(self.container_id, MOUNT_POINT)


class HostFileReader:
    """Reads files from an environment's host; administrators only.

    Create, read and remove each make their own Engine request, so none may run inside a
    database transaction.
    """

    def __init__(self, client: EngineClient, *, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._clock = clock

    @asynccontextmanager
    async def session(
        self, actor: Principal, working_dir: str, *, image: str
    ) -> AsyncIterator[HostFileSession]:
        """Sweep leftovers, create a helper over `working_dir`, and remove it on exit.

        `image` must already be on the host. The helper is removed when the block ends by
        return, error or cancellation, and a create cancelled in flight is removed once it
        returns; `session.removed` reports whether that succeeded. HelperMountRefused when the
        Engine refuses the working directory, which it does for a path that does not exist.
        """
        require_administrator(actor)
        check_working_directory(working_dir)
        leftovers = await self.remove_leftovers()
        container_id = await self._create_helper(helper_body(image, working_dir))
        session = HostFileSession(self._client, container_id, leftovers)
        try:
            yield session
        finally:
            session.close()
            session.removed = await self._discard(container_id)

    @asynccontextmanager
    async def volume_session(
        self, actor: Principal, volume: str, *, image: str
    ) -> AsyncIterator[VolumeHelper]:
        """Sweep leftovers, create a helper that mounts `volume` read-only, and remove it on exit.

        The helper is removed together with any anonymous volume its image declared, never the
        mounted volume, and is exempt from the leftover sweep while the block runs. `image` must
        already be on the host. HelperMountRefused when the Engine refuses the mount.
        """
        require_administrator(actor)
        leftovers = await self.remove_leftovers()
        container_id = await self._create_helper(volume_helper_body(image, volume), volumes=True)
        helper = VolumeHelper(self._client, container_id, leftovers)
        _IN_USE.add(container_id)
        try:
            yield helper
        finally:
            try:
                helper.removed = await self._discard(container_id, volumes=True)
            finally:
                _IN_USE.discard(container_id)

    async def read(
        self,
        actor: Principal,
        working_dir: str,
        relative_paths: Sequence[str],
        *,
        image: str,
        limit: int,
    ) -> HostFilesResult:
        """Read every path below `working_dir` with one helper container."""
        async with self.session(actor, working_dir, image=image) as session:
            results = await session.read_all(relative_paths, limit=limit)
        return HostFilesResult(
            helper_container=session.container_id,
            results=results,
            leftovers=session.leftovers,
            helper_removed=session.removed,
        )

    async def remove_leftovers(self) -> tuple[HelperLeftover, ...]:
        """Remove helpers left in state `created` for over 10 minutes; report every other one.

        A helper is removed only when it also carries a helper name. A removal the Engine
        refuses is reported as not removed.
        """
        listed = await self._client.containers.list(
            all=True, filters={"label": [f"{HELPER_ROLE_LABEL}={HELPER_ROLE}"]}
        )
        now = self._clock()
        found: list[HelperLeftover] = []
        for summary in listed:
            stale = (
                summary.id not in _IN_USE
                and summary.state == _CREATED_STATE
                and now - summary.created > LEFTOVER_AGE_SECONDS
                and any(_HELPER_NAME.fullmatch(name.removeprefix("/")) for name in summary.names)
            )
            removed = False
            if stale:
                try:
                    await self._client.containers.remove(summary.id, force=False, volumes=True)
                except EngineError as error:
                    logger.warning(
                        "host_file_helper_sweep_failed",
                        extra={
                            "environment_id": error.environment_id,
                            "error": type(error).__name__,
                        },
                    )
                else:
                    removed = True
            found.append(HelperLeftover(summary.id, summary.state, removed))
        return tuple(found)

    async def _create_helper(self, body: dict[str, Any], *, volumes: bool = False) -> str:
        name = f"{HELPER_NAME_PREFIX}{secrets.token_hex(_NAME_RANDOM_BYTES)}"
        return await self._create(body, name, volumes)

    async def _create(self, body: dict[str, Any], name: str, volumes: bool) -> str:
        creating = asyncio.ensure_future(self._client.containers.create(body, name=name))
        try:
            return await asyncio.shield(creating)
        except asyncio.CancelledError:
            await self._remove_created(creating, volumes)
            raise
        except EngineError as error:
            if error.status_code == _BAD_REQUEST:
                raise HelperMountRefused from None
            raise

    async def _remove_created(self, creating: asyncio.Future[str], volumes: bool) -> None:
        while not creating.done():
            with suppress(asyncio.CancelledError):
                await asyncio.wait({creating})
        if creating.cancelled() or creating.exception() is not None:
            return
        await self._discard(creating.result(), volumes=volumes)

    async def _discard(self, container_id: str, *, volumes: bool = False) -> bool:
        removal = asyncio.ensure_future(self._remove(container_id, volumes))
        try:
            await asyncio.shield(removal)
        except asyncio.CancelledError:
            while not removal.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.wait({removal})
            raise
        return removal.result()

    async def _remove(self, container_id: str, volumes: bool) -> bool:
        try:
            async with asyncio.timeout(REMOVE_SECONDS):
                await self._client.containers.remove(container_id, force=False, volumes=volumes)
        except (EngineError, TimeoutError) as error:
            logger.warning(
                "host_file_helper_remove_failed",
                extra={
                    "environment_id": self._client.environment_id,
                    "error": type(error).__name__,
                },
            )
            return False
        return True
