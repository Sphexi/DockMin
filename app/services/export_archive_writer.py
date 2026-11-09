"""Writes a stack or container export as one gzip stream: manifest, documents, then volumes."""

import tarfile
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass

from app.services.export_build import PreparedExport
from app.services.volume_export_writer import (
    GzipTarStream,
    SkippedMember,
    VolumeCopier,
    add_document,
)
from app.services.volume_manifest import MANIFEST_NAME as VOLUME_MANIFEST_NAME
from app.services.volume_manifest import VolumeManifest
from app.services.volume_manifest import manifest_bytes as volume_manifest_bytes

type VolumeOpener = Callable[[], AbstractAsyncContextManager[AsyncIterator[bytes]]]


@dataclass(frozen=True, slots=True)
class VolumeSource:
    """Where one volume's data comes from.

    `open` returns an async context manager that yields the Engine's tar of the directory named
    `root`. It is entered when the volume's turn comes and exited when that volume ends,
    whether it completed, failed or the stream was closed, so a helper can be created on enter
    and removed on exit. Both run on the event loop, in separate tasks.
    """

    root: str
    open: VolumeOpener


@dataclass(frozen=True, slots=True)
class VolumeResult:
    """What was copied of one volume."""

    prefix: str
    entries: int
    skipped_count: int
    skipped: tuple[SkippedMember, ...]


class ExportArchiveStream(GzipTarStream):
    """The export archive as gzip chunks; `volumes[i]` supplies `prepared.manifest.volumes[i]`.

    Members are written in this order: `manifest.json`, the documents, then for each volume its
    manifest, its files under `<prefix>/data/` and `<prefix>/skipped.json`. Volume data is read
    only as it is written, never held. StreamMalformed when a volume's tar is not that of one
    directory; any error of a source or its context manager is raised as it came. `volume_results`
    fills in as volumes complete.
    """

    def __init__(self, prepared: PreparedExport, volumes: Sequence[VolumeSource] = ()) -> None:
        super().__init__()
        manifest = prepared.manifest
        if len(volumes) != len(manifest.volumes):
            raise ValueError("a source is needed for each volume in the manifest")
        if {item.member for item in prepared.documents} != set(manifest.documents()):
            raise ValueError("the documents do not match the manifest")
        self._prepared = prepared
        self._sources = tuple(volumes)
        self.volume_results: list[VolumeResult] = []

    def _write_members(self, out: tarfile.TarFile) -> None:
        manifest = self._prepared.manifest_document
        add_document(out, manifest.member, manifest.content)
        for document in self._prepared.documents:
            add_document(out, document.member, document.content)
        for section, source in zip(self._prepared.manifest.volumes, self._sources, strict=True):
            self._write_volume(out, section.prefix, section.manifest, source)

    def _write_volume(
        self, out: tarfile.TarFile, prefix: str, manifest: VolumeManifest, source: VolumeSource
    ) -> None:
        directory = f"{prefix}/"
        add_document(out, f"{directory}{VOLUME_MANIFEST_NAME}", volume_manifest_bytes(manifest))
        copier = VolumeCopier(source.root, directory)
        held = AsyncExitStack()
        chunks = self._call(_enter(held, source))
        try:
            copier.copy(self._chunks_of(chunks), out)
            copier.write_skipped(out)
        except BaseException as error:
            self._call_always(held.__aexit__(type(error), error, error.__traceback__))
            raise
        self._call_always(held.aclose())
        self.volume_results.append(
            VolumeResult(prefix, copier.entries, copier.skipped_count, tuple(copier.skipped))
        )


async def _enter(held: AsyncExitStack, source: VolumeSource) -> AsyncIterator[bytes]:
    return await held.enter_async_context(source.open())
