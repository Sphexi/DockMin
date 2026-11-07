"""Reads a Compose project's own files from its host through a helper container."""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.auth.principal import Principal
from app.compose_cli.errors import ComposeError
from app.docker_engine.errors import NotFound
from app.docker_engine.models import ContainerSummary
from app.services.access import COMPOSE_PROJECT_LABEL, canonical_stack_key
from app.services.compose_read_files import Collected, collect_references
from app.services.compose_read_inputs import (
    CONFIG_FILES_LABEL,
    ENVIRONMENT_FILE_LABEL,
    WORKING_DIR_LABEL,
    ProjectLocation,
    locate_project,
    parse_environment_file,
    variables_of,
)
from app.services.host_files import (
    FileRead,
    FileRefusal,
    HelperMountRefused,
    HostFileReader,
    HostFileSession,
)
from app.services.stack_document import (
    MAX_COMPOSE_BYTES,
    MAX_FILE_BYTES,
    ComposeDocumentRejected,
    decode_compose,
    decode_file,
)
from app.services.stack_read_result import NeedsFile, ReadOutcome, finish

__all__ = ["MERGED_LOSSES", "ComposeFileReader", "FilesRead", "MergeFiles"]

MERGED_LOSSES: Final = (
    "The config files were merged into one file; comments, anchors and the original layout of "
    "each are not kept.",
)


class MergeFiles(Protocol):
    """Merges Compose files, as the Compose CLI does, and returns the merged YAML."""

    async def __call__(self, name: str, texts: Sequence[str]) -> str:
        """Merge `texts`, which are in the order they were given to Compose."""
        ...


@dataclass(frozen=True, slots=True)
class FilesRead:
    """What reading a project's files produced and what the helper container did."""

    outcome: ReadOutcome
    helper_container: str | None = None
    files_read: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    helper_removed: bool | None = None


class _Tracker:
    """Reads through the helper and remembers which paths were read and which were refused."""

    def __init__(self, session: HostFileSession) -> None:
        self._session = session
        self.read_paths: list[str] = []
        self.refused: list[tuple[str, str]] = []

    async def read(self, relative_path: str, *, limit: int) -> FileRead:
        found = await self._session.read(relative_path, limit=limit)
        if isinstance(found, FileRefusal):
            self.refused.append((relative_path, found.reason.value))
        else:
            self.read_paths.append(relative_path)
        return found


class ComposeFileReader:
    """Reads the config, referenced and environment files a project's labels name."""

    def __init__(self, reader: HostFileReader, merge: MergeFiles) -> None:
        self._reader = reader
        self._merge = merge

    async def read(
        self,
        actor: Principal,
        project: str,
        containers: Sequence[ContainerSummary],
        protects: Callable[[str], bool],
    ) -> FilesRead:
        """Return the project's files as a stack, or why they could not be read.

        The helper is created from the image of one of the project's containers and removed
        before this returns. Engine failures other than a refused file propagate;
        a missing helper image is a reason.
        """
        location = locate_project([c.labels for c in containers], protects)
        if isinstance(location, NeedsFile):
            return FilesRead(location)
        image = next((c.image_id for c in containers if c.image_id), "")
        if not image:
            return FilesRead(NeedsFile("helper_image_missing", COMPOSE_PROJECT_LABEL))
        try:
            async with self._reader.session(actor, location.working_dir, image=image) as session:
                tracker = _Tracker(session)
                outcome = await self._outcome(project, location, tracker)
        except NotFound:
            return FilesRead(NeedsFile("helper_image_missing", COMPOSE_PROJECT_LABEL))
        except HelperMountRefused:
            return FilesRead(NeedsFile("working_directory_unmountable", WORKING_DIR_LABEL))
        return FilesRead(
            outcome,
            session.container_id,
            tuple(tracker.read_paths),
            tuple(tracker.refused),
            session.removed,
        )

    async def _outcome(
        self, project: str, location: ProjectLocation, tracker: _Tracker
    ) -> ReadOutcome:
        documents: list[str] = []
        for path in location.config_files:
            found = await tracker.read(path, limit=MAX_COMPOSE_BYTES)
            if isinstance(found, FileRefusal):
                return NeedsFile(found.reason.value, path)
            try:
                documents.append(decode_compose(found.content))
            except ComposeDocumentRejected as rejected:
                return NeedsFile(rejected.reason, path)
        values = await self._variables(location, tracker)
        if isinstance(values, NeedsFile):
            return values
        collected = await collect_references(tracker, location.working_dir, documents)
        if isinstance(collected, NeedsFile):
            return collected
        if len(collected.texts) == 1:
            return await asyncio.to_thread(
                finish,
                collected.texts[0],
                kind="compose",
                source="read",
                files=collected.files,
                variables=variables_of(values),
            )
        return await self._merged(project, collected, values)

    async def _variables(
        self, location: ProjectLocation, tracker: _Tracker
    ) -> dict[str, str] | NeedsFile:
        values: dict[str, str] = {}
        for path in location.environment_files:
            found = await tracker.read(path, limit=MAX_FILE_BYTES)
            if isinstance(found, FileRefusal):
                return NeedsFile(found.reason.value, ENVIRONMENT_FILE_LABEL)
            try:
                text = decode_file(found.content, field=path)
            except ComposeDocumentRejected as rejected:
                return NeedsFile(rejected.reason, ENVIRONMENT_FILE_LABEL)
            parsed = parse_environment_file(text, path)
            if isinstance(parsed, NeedsFile):
                return parsed
            values.update(parsed)
        return values

    async def _merged(
        self, project: str, collected: Collected, values: dict[str, str]
    ) -> ReadOutcome:
        if collected.names_compose_files:
            return NeedsFile("merge_unsupported", CONFIG_FILES_LABEL)
        for text in collected.texts:
            checked = await asyncio.to_thread(
                finish, text, kind="compose", source="read", files=collected.files
            )
            if isinstance(checked, NeedsFile):
                return checked
        try:
            merged = await self._merge(canonical_stack_key(project), collected.texts)
        except ComposeError:
            return NeedsFile("merge_failed", CONFIG_FILES_LABEL)
        return await asyncio.to_thread(
            finish,
            merged,
            kind="compose",
            source="merged",
            files=collected.files,
            variables=variables_of(values),
            losses=MERGED_LOSSES,
        )
