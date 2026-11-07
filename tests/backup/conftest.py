"""One populated instance and one archive of it per test module, built synchronously."""

import asyncio
from collections.abc import Iterator

import pytest

from app.backup import archive, paths, producer
from tests.backup.support import Stored, World, clock, make_world, read_members


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[World]:
    built = asyncio.run(make_world(tmp_path_factory.mktemp("world")))
    yield built
    asyncio.run(built.database.aclose())


@pytest.fixture(scope="module")
def stored(world: World) -> Stored:
    workdir = paths.new_work_directory(world.data_dir)
    produced = asyncio.run(
        producer.produce_archive(world.database, world.vault, workdir, clock=clock)
    )
    manifest, raw = archive.read_manifest(produced.path)
    members = {name: data for name, (_, data) in read_members(produced.path).items()}
    return Stored(produced.path, manifest, raw, produced.sha256, members)
