"""Validation of a container export archive: its kind, its volumes and the tar rules."""

from pathlib import Path

import pytest

from app.services.stack_import_archive import (
    StackArchiveRefused,
    inspect_container_archive,
    stack_volume_tar,
)
from tests.container_import_support import (
    FIXTURE,
    NOTE,
    NOTE_FILE,
    VOLUME,
    container_archive,
    with_manifest,
)
from tests.stack_import_support import build_archive


def stored(tmp_path: Path, archive: bytes) -> Path:
    path = tmp_path / "upload.tar.gz"
    path.write_bytes(archive)
    return path


async def test_a_container_archive_is_summarised_with_its_form_and_volume(tmp_path: Path) -> None:
    path = stored(tmp_path, await container_archive())

    summary = inspect_container_archive(path)

    manifest = summary.manifest
    assert manifest.kind == "container" and manifest.container is not None
    assert [entry.name for entry in manifest.container.environment] == ["MODE", "DB_PASSWORD"]
    assert [item.manifest.name for item in summary.volumes] == [VOLUME]
    assert summary.volumes[0].summary.bytes == len(NOTE)
    assert (summary.compose_text, summary.files) == ("", {})
    members = b"".join(stack_volume_tar(path, summary.volumes[0].prefix))
    assert NOTE_FILE.encode() in members


def test_the_format_version_1_container_fixture_is_read_in_full() -> None:
    summary = inspect_container_archive(FIXTURE)

    assert summary.manifest.format_version == 1 and summary.manifest.secrets == "sealed"
    assert summary.volumes[0].summary.members > 0


async def test_a_stack_archive_is_refused_as_a_container(tmp_path: Path) -> None:
    path = stored(tmp_path, await build_archive())

    with pytest.raises(StackArchiveRefused) as raised:
        inspect_container_archive(path)

    assert "holds a stack" in raised.value.message


async def test_a_newer_format_version_is_refused_and_named(tmp_path: Path) -> None:
    path = stored(tmp_path, with_manifest(await container_archive(), format_version=7))

    with pytest.raises(StackArchiveRefused) as raised:
        inspect_container_archive(path)

    assert raised.value.version == 7


async def test_volume_members_the_manifest_does_not_declare_are_refused(tmp_path: Path) -> None:
    path = stored(tmp_path, with_manifest(await container_archive(), volumes=[]))

    with pytest.raises(StackArchiveRefused):
        inspect_container_archive(path)
