"""Descriptions, volume sources and readers for the stack and container export tests."""

import gzip
import io
import tarfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.services.export_archive_writer import ExportArchiveStream, VolumeSource
from app.services.export_build import (
    ContextInput,
    EnvironmentInput,
    ExportDescription,
    FileInput,
    PreparedExport,
    VariableInput,
    prepare_export,
)
from app.services.export_manifest import ImageReference
from app.services.export_sealing import MINIMUM_COST
from app.services.volume_manifest import VolumeManifest, build_manifest
from tests.volume_export_support import ROOT, chunked, volume_tar

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "export_archive" / "format_version_1.tar.gz"
)
FIXTURE_PASSPHRASE = "fixture passphrase 1"
FIXTURE_SECRET_NAME = "variable:DB_PASSWORD"
FIXTURE_SECRET_VALUE = b"fixture-db-password"
PASSPHRASE = "sentinel passphrase 1"
SECRET_VALUE = "sentinel-secret-value"
SECRET_FILE_CONTENT = b"sentinel-secret-file-content"
SECRET_ENVIRONMENT_VALUE = "sentinel-environment-secret"
PLAIN_VALUE = "plain-value-visible"
COMPOSE = "services:\n  web:\n    image: nginx:1.27\n"
FILE_NAME = "nginx.conf"
FILE_CONTENT = b"worker_processes 1;\n"
EXPORTED_AT = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32
VOLUME_NAME = "shop-data"
VOLUME_NOTE = b"hello\n"
VOLUME_FILE = "notes/a.txt"


def volume_manifest(name: str = VOLUME_NAME) -> VolumeManifest:
    return build_manifest(
        exported_at=EXPORTED_AT,
        instance_version="0.2.2",
        name=name,
        driver="local",
        labels={"team": "core"},
        options={},
    )


def stack_description(
    *, secret: bool = True, volumes: tuple[VolumeManifest, ...] = (), **changes: object
) -> ExportDescription:
    fields: dict[str, object] = {
        "kind": "compose_stack",
        "name": "shop",
        "description": "Compose stack shop",
        "instance_version": "0.2.2",
        "exported_at": EXPORTED_AT,
        "compose_file": COMPOSE,
        "variables": (
            VariableInput("PLAIN", False, PLAIN_VALUE),
            *((VariableInput("DB_PASSWORD", True, SECRET_VALUE),) if secret else ()),
        ),
        "files": (
            FileInput(FILE_NAME, False, FILE_CONTENT),
            *((FileInput("tls.key", True, SECRET_FILE_CONTENT),) if secret else ()),
        ),
        "contexts": (ContextInput("app", b"context-archive-bytes", False, 1, 20),),
        "images": (ImageReference(reference="nginx:1.27", tag="1.27", digest=DIGEST),),
        "volumes": volumes,
    }
    fields.update(changes)
    return ExportDescription(**fields)  # type: ignore[arg-type]


def container_description(**changes: object) -> ExportDescription:
    fields: dict[str, object] = {
        "kind": "container",
        "name": "web",
        "description": "Container web",
        "instance_version": "0.2.2",
        "exported_at": EXPORTED_AT,
        "form": {"image": "nginx:1.27", "name": "web"},
        "environment": (
            EnvironmentInput("MODE", False, "production"),
            EnvironmentInput("TOKEN", True, SECRET_ENVIRONMENT_VALUE),
        ),
    }
    fields.update(changes)
    return ExportDescription(**fields)  # type: ignore[arg-type]


def prepared(description: ExportDescription, passphrase: str | None = PASSPHRASE) -> PreparedExport:
    return prepare_export(description, passphrase, cost=MINIMUM_COST)


class FakeVolume:
    """A volume source over a tar, recording how its context manager was entered and left."""

    def __init__(self, tar: bytes | None = None, *, fail_after: int | None = None) -> None:
        self.tar = tar if tar is not None else volume_tar({VOLUME_FILE: VOLUME_NOTE})
        self.fail_after = fail_after
        self.entered = 0
        self.exits: list[type[BaseException] | None] = []

    def source(self) -> VolumeSource:
        return VolumeSource(ROOT, self._open)

    def _open(self) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        return self._manage()

    @asynccontextmanager
    async def _manage(self) -> AsyncIterator[AsyncIterator[bytes]]:
        self.entered += 1
        try:
            yield self._chunks()
        except BaseException as error:
            self.exits.append(type(error))
            raise
        else:
            self.exits.append(None)

    async def _chunks(self) -> AsyncIterator[bytes]:
        sent = 0
        async for chunk in chunked(self.tar, 1024):
            if self.fail_after is not None and sent >= self.fail_after:
                raise ConnectionError("the source failed")
            sent += 1
            yield chunk


async def write_archive(stream: ExportArchiveStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


def members_of(archive: bytes) -> dict[str, tuple[tarfile.TarInfo, bytes]]:
    found: dict[str, tuple[tarfile.TarInfo, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as tar:
        for member in tar:
            source = tar.extractfile(member) if member.isreg() else None
            found[member.name] = (member, source.read() if source is not None else b"")
    return found
