"""Start-up of the Compose side: the binary and its verdict, stack file directories, TLS targets."""

import asyncio
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from app.compose_cli.binary import ComposeBinary, locate
from app.compose_cli.buildx import BuildxCli, locate_buildx
from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.errors import (
    ComposeBinaryInvalid,
    ComposeError,
    ComposeUnavailable,
    ComposeVersionUnsupported,
)
from app.compose_cli.parsers import FLOOR, ComposeVersion
from app.compose_cli.target import ComposeTarget, InvalidTarget, TargetMaterial, target_for
from app.compose_cli.workdir import configure_compose_workdir, write_private
from app.crypto.envelope import DecryptionError
from app.db.repositories.environment import Environment
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.stack import StackKind
from app.logging import get_logger
from app.netcheck import BoundedResolver
from app.services.environment_connector import MaterialUnavailable
from app.services.object_inspector import EnvironmentUnavailable
from app.services.stack_deploy import (
    StackRejected,
    delivered_sources,
    file_secret_values,
)
from app.services.stack_document import (
    FILE_NAME,
    CompanionFile,
    ComposeDocumentRejected,
    FileUses,
    ServiceBuildCheck,
    precheck,
)

__all__ = [
    "ORPHAN_MIN_AGE_SECONDS",
    "STACKS_DIRECTORY",
    "ComposeBinaryInvalid",
    "ComposeRuntime",
    "ComposeStatus",
    "Materialised",
    "StackFiles",
    "build_target",
    "load_material",
]

logger = get_logger(__name__)

STACKS_DIRECTORY: Final = "stacks"
COMPOSE_FILE_NAME: Final = "compose.yaml"
DETECTION_RETRY_SECONDS: Final = 60.0
ORPHAN_MIN_AGE_SECONDS: Final = 600.0

_WRITTEN_ROLES: Final = frozenset({"env", "compose"})

_PRIVATE_DIRECTORY_MODE: Final = 0o700
_TOKEN_BYTES: Final = 16
_TOKEN_LENGTH: Final = 32
_HEX: Final = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class Materialised:
    """A project directory holding the files one invocation reads; `uses` is where each is named.

    `project` carries the sources Compose receives through its environment.
    """

    project: ComposeProject
    uses: FileUses


class StackFiles:
    """The directories that hold each stack's working copy of its Compose file and companions."""

    def __init__(self, base: Path) -> None:
        self.base = base

    @classmethod
    def prepare(cls, data_dir: Path) -> "StackFiles":
        """Create the private base beneath the data directory; call once at startup.

        Everything but each project's Compose file is removed from every project directory,
        because a crash can leave a secret file behind.
        """
        base = data_dir / STACKS_DIRECTORY
        if base.is_symlink():
            raise ValueError("The stacks directory must not be a symlink.")
        base.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        base.chmod(_PRIVATE_DIRECTORY_MODE)
        resolved = base.resolve()
        _clear_projects(resolved)
        return cls(resolved)

    @staticmethod
    def new_token() -> str:
        """Return a fresh directory token; nothing in a path is ever taken from user input."""
        return secrets.token_hex(_TOKEN_BYTES)

    def project(self, name: str, token: str) -> ComposeProject:
        """Return the Compose project of a stack whose directory token is `token`."""
        if len(token) != _TOKEN_LENGTH or not set(token) <= _HEX:
            raise ValueError("A project directory token is 32 lower-case hex digits.")
        directory = self.base / token
        return ComposeProject(name, directory, directory / COMPOSE_FILE_NAME)

    @asynccontextmanager
    async def materialise(
        self,
        project: ComposeProject,
        text: str,
        files: Sequence[CompanionFile] = (),
        *,
        kind: StackKind = "compose",
        builds: ServiceBuildCheck | None = None,
    ) -> AsyncIterator[Materialised]:
        """Check the file and its companions again, write what Compose reads, and clean up.

        StackRejected when the pre-check refuses; nothing is written then. The project directory
        is reconciled first: anything that is not the Compose file or a plain companion file is
        removed without being followed. `builds` admits a service's `build` section, as for
        `precheck`. The files are written atomically at `0600`. A config or
        secret read from a file is not written: its content reaches Compose through the
        environment. On exit, which cancellation cannot cut short, the secret files this call
        wrote are unlinked.
        """
        try:
            uses = await asyncio.to_thread(precheck, text, kind=kind, files=files, builds=builds)
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None
        by_name = {item.name: item for item in files}
        wanted = tuple(
            by_name[name]
            for name, found in uses.items()
            if any(use.role in _WRITTEN_ROLES for use in found)
        )
        secret_names = tuple(item.name for item in wanted if item.secret)
        attached = replace(
            project,
            sources=delivered_sources(uses, files),
            sensitive=file_secret_values(files, uses),
        )
        try:
            await _finish(asyncio.to_thread(_materialise, project, text.encode("utf-8"), wanted))
            yield Materialised(attached, uses)
        finally:
            await _finish(asyncio.to_thread(_remove_files, project.directory, secret_names))

    @asynccontextmanager
    async def scratch(
        self,
        name: str,
        text: str,
        files: Sequence[CompanionFile] = (),
        *,
        kind: StackKind | None = None,
        builds: ServiceBuildCheck | None = None,
    ) -> AsyncIterator[ComposeProject]:
        """Yield a project in a throwaway directory holding the candidate file and its companions.

        The directory is never a stack's own, and is removed on exit. A config or secret read
        from a file is not written: its content reaches Compose through the environment.
        Without `kind`, the file is read as a Compose file and then as a Swarm one.
        StackRejected, carrying the first refusal, when neither pre-check accepts it.
        """
        uses = await _uses_of(text, files, kind, builds)
        token = self.new_token()
        project = replace(
            self.project(name, token),
            sources=delivered_sources(uses, files),
            sensitive=file_secret_values(files, uses),
        )
        written = tuple(
            item
            for item in files
            if any(use.role in _WRITTEN_ROLES for use in uses.get(item.name, ()))
        )
        try:
            await _finish(asyncio.to_thread(_materialise, project, text.encode("utf-8"), written))
            yield project
        finally:
            await _finish(self.discard(token))

    async def discard(self, token: str) -> bool:
        """Delete a project directory; a failure is logged and left to the next sweep."""
        project = self.project("x", token)
        return await asyncio.to_thread(_remove_directory, project.directory)

    async def sweep(self, known: Collection[str], *, min_age_seconds: float = 0.0) -> int:
        """Delete every directory whose token no stack holds, and return how many went.

        An entry younger than `min_age_seconds` is kept: a stack being created writes its
        directory before its row exists.
        """
        return await asyncio.to_thread(_sweep, self.base, frozenset(known), min_age_seconds)


async def _uses_of(
    text: str,
    files: Sequence[CompanionFile],
    kind: StackKind | None,
    builds: ServiceBuildCheck | None = None,
) -> FileUses:
    kinds: tuple[StackKind, ...] = ("compose", "swarm") if kind is None else (kind,)
    first: ComposeDocumentRejected | None = None
    for candidate in kinds:
        try:
            return await asyncio.to_thread(
                precheck, text, kind=candidate, files=files, builds=builds
            )
        except ComposeDocumentRejected as rejected:
            first = first or rejected
    if first is None:
        raise RuntimeError("no pre-check kind was tried")
    raise StackRejected(first.fields, first.reason, lines=first.lines) from None


async def _finish[T](work: Awaitable[T]) -> T:
    """Run work to its end however many times the caller is cancelled, then return its result."""
    task = asyncio.ensure_future(work)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            continue
    if interrupted:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


def _materialise(project: ComposeProject, text: bytes, files: Sequence[CompanionFile]) -> None:
    directory = project.directory
    if directory.is_symlink():
        raise OSError("A project directory must not be a symlink.")
    directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE, exist_ok=True)
    directory.chmod(_PRIVATE_DIRECTORY_MODE)
    _reconcile(directory, {project.file.name, *(item.name for item in files if not item.secret)})
    written: list[str] = []
    try:
        _write_atomic(project.file, text)
        for item in files:
            if FILE_NAME.fullmatch(item.name) is None or item.name == project.file.name:
                raise ValueError("A companion file name is outside the permitted grammar.")
            _write_atomic(directory / item.name, item.content.encode("utf-8"))
            if item.secret:
                written.append(item.name)
    except BaseException:
        _remove_files(directory, written)
        raise


def _write_atomic(target: Path, data: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}")
    try:
        write_private(temporary, data)
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _reconcile(directory: Path, keep: Collection[str]) -> None:
    """Remove every entry that is not a regular file named in `keep`; nothing is followed."""
    with os.scandir(directory) as scanned:
        entries = list(scanned)
    for entry in entries:
        if entry.name in keep and entry.is_file(follow_symlinks=False):
            continue
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            Path(entry.path).unlink()


def _remove_files(directory: Path, names: Collection[str]) -> None:
    for name in names:
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            logger.warning("stack_file_cleanup_failed", extra={"file": name})


def _clear_projects(base: Path) -> None:
    for child in base.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            _reconcile(child, {COMPOSE_FILE_NAME})
        except OSError:
            logger.warning("stack_directory_clear_failed", extra={"directory": child.name})


def _remove_directory(directory: Path) -> bool:
    try:
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)
    except OSError:
        logger.warning("stack_directory_cleanup_failed", extra={"directory": directory.name})
        return False
    return True


def _sweep(base: Path, known: frozenset[str], min_age: float) -> int:
    removed = 0
    now = time.time()
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.name in known:
            continue
        try:
            if min_age and now - entry.lstat().st_mtime < min_age:
                continue
        except OSError:
            continue
        if _remove_directory(entry):
            removed += 1
    return removed


@dataclass(frozen=True, slots=True)
class ComposeStatus:
    """What the pages say about Compose: whether it can run, what is installed, what is needed."""

    ready: bool
    installed: str | None
    required: str
    message: str | None


class ComposeRuntime:
    """Finds the Compose binary once, checks its version against the floor, and hands out the CLI.

    A missing or too-old Compose does not stop the application; every Compose operation asks
    `ready()` first and is refused with a product error that names what is wrong.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        configured: Path | None,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        floor: ComposeVersion = FLOOR,
        on_version: Callable[[str | None], None] | None = None,
    ) -> None:
        self.work_base = configure_compose_workdir(data_dir)
        self.files = StackFiles.prepare(data_dir)
        self.floor = floor
        self.version: ComposeVersion | None = None
        self.timeout_seconds = timeout_seconds
        self._binary: ComposeBinary | None = locate(configured)
        self._cli: ComposeCli | None = None
        self._clock = clock
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()
        self._on_version = on_version
        self._retry: asyncio.Future[None] | None = None
        self._resolver = BoundedResolver()
        self.buildx = BuildxCli(None, resolver=self._resolver)

    def find_buildx(self, configured: Path | None) -> None:
        """Locate the buildx binary; without a trusted one, builds are unavailable."""
        self.buildx = BuildxCli(locate_buildx(configured), resolver=self._resolver)

    @property
    def version_text(self) -> str | None:
        """Return the installed version as Compose printed it, or None when none was read."""
        return None if self.version is None else self.version.text

    @property
    def supported(self) -> bool:
        """Report whether a version was read and it meets the floor."""
        return self.version is not None and self.version.at_least(self.floor)

    @property
    def binary(self) -> ComposeBinary | None:
        """Return the located binary, or None."""
        return self._binary

    def use_binary(self, binary: ComposeBinary) -> None:
        """Replace the binary and forget the detected version; for a harness that fakes Compose."""
        self._binary = binary
        self._cli = None
        self.version = None
        self._attempted_at = None
        self._announce()

    async def detect(self) -> None:
        """Read the installed version once and log the verdict."""
        async with self._lock:
            await self._detect()

    def status(self) -> ComposeStatus:
        """Describe Compose from what is already known, without waiting on a process.

        While no version has been read, a retry is started in the background at most once a
        minute, so a later call sees the result.
        """
        if self.version is None:
            self._start_retry()
            return ComposeStatus(False, None, self.floor.text, str(ComposeUnavailable()))
        if not self.supported:
            error = ComposeVersionUnsupported(self.version.text, self.floor.text)
            return ComposeStatus(False, self.version.text, self.floor.text, str(error))
        return ComposeStatus(True, self.version.text, self.floor.text, None)

    async def aclose(self) -> None:
        """Cancel a background retry and stop the name-lookup threads."""
        retry, self._retry = self._retry, None
        if retry is not None:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        self._resolver.close()

    async def ready(self) -> ComposeCli:
        """Return the CLI, retrying detection at most once a minute after a failure.

        Raises ComposeUnavailable when there is no usable binary and ComposeVersionUnsupported
        when the installed Compose is older than the floor.
        """
        async with self._lock:
            if self.version is None and self._may_retry():
                await self._detect()
            if self._cli is None or self.version is None:
                raise ComposeUnavailable
            if not self.supported:
                raise ComposeVersionUnsupported(self.version.text, self.floor.text)
            return self._cli

    def _start_retry(self) -> None:
        if self._retry is not None or self._binary is None or not self._may_retry():
            return
        try:
            self._retry = asyncio.ensure_future(self._retry_detection())
        except RuntimeError:
            self._retry = None

    async def _retry_detection(self) -> None:
        try:
            async with self._lock:
                if self.version is None and self._may_retry():
                    await self._detect()
        finally:
            self._retry = None

    def _announce(self) -> None:
        if self._on_version is not None:
            self._on_version(self.version_text)

    def _may_retry(self) -> bool:
        return (
            self._attempted_at is None
            or self._clock() - self._attempted_at >= DETECTION_RETRY_SECONDS
        )

    async def _detect(self) -> None:
        self._attempted_at = self._clock()
        if self._binary is None:
            logger.warning("compose_binary_not_found")
            return
        cli = ComposeCli(
            self._binary,
            work_base=self.work_base,
            operation_timeout=self.timeout_seconds,
            resolver=self._resolver,
        )
        try:
            version = await cli.version()
        except (ComposeError, OSError) as error:
            logger.warning("compose_version_unreadable", extra={"error": type(error).__name__})
            return
        self._cli = cli
        self.version = version
        self._announce()
        if version.at_least(self.floor):
            logger.info("compose_detected", extra={"version": version.text})
        else:
            logger.warning(
                "compose_below_floor",
                extra={"installed": version.text, "required": self.floor.text},
            )


async def load_material(tls: EnvironmentTlsRepository, environment_id: int) -> TargetMaterial:
    """Read the stored PEM text and decrypt the client key.

    Raises MaterialUnavailable when the material is incomplete or the key cannot be opened.
    """
    stored = await tls.get(environment_id)
    if (
        stored is None
        or not stored.ca_certificate
        or not stored.client_certificate
        or not stored.client_key_ciphertext
    ):
        raise MaterialUnavailable
    try:
        key = await tls.reveal_client_key(environment_id)
    except DecryptionError:
        logger.error("compose_tls_key_unreadable", extra={"environment_id": environment_id})
        raise MaterialUnavailable from None
    if not key:
        raise MaterialUnavailable
    return TargetMaterial(stored.ca_certificate, stored.client_certificate, key)


async def build_target(environment: Environment, tls: EnvironmentTlsRepository) -> ComposeTarget:
    """Return where a Compose invocation connects for this environment.

    Raises EnvironmentUnavailable("credentials") when the stored connection cannot be used.
    """
    try:
        material = (
            await load_material(tls, environment.id)
            if environment.connection_kind == "tcp_tls"
            else None
        )
        return target_for(environment, material)
    except (MaterialUnavailable, InvalidTarget):
        raise EnvironmentUnavailable("credentials") from None
