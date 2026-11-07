"""No archive holds the encryption key, in any spelling, in any member, or under any name."""

import base64
import gzip
import logging
from pathlib import Path

import pytest

from app.backup import paths, producer
from app.backup.manifest import ARCHIVE_MEMBER, ProductionFailed
from app.crypto.keys import key_forms
from tests.backup.support import NEW_STACK, Stored, World, clock, make_world
from tests.crypto.helpers import FIRST_KEY, SECOND_KEY, emitted, leaked

KEYS = (FIRST_KEY, SECOND_KEY)


def spellings(key: bytes) -> list[bytes]:
    texts = [*key_forms(key), key.hex().upper()]
    return [key, *(text.encode("ascii") for text in texts)]


def hits(blob: bytes, keys: tuple[bytes, ...] = KEYS) -> list[bytes]:
    return [spelling for key in keys for spelling in spellings(key) if spelling in blob]


def test_the_search_finds_a_key_when_one_is_present() -> None:
    planted = b"x" * 100 + base64.b64encode(SECOND_KEY) + b"y"

    assert base64.b64encode(SECOND_KEY) in hits(planted)


def test_no_spelling_of_either_key_appears_in_the_archive_its_stream_or_any_member(
    stored: Stored,
) -> None:
    compressed = stored.path.read_bytes()
    stream = gzip.decompress(compressed)

    found = {
        "compressed": hits(compressed),
        "stream": hits(stream),
        **{name: hits(data) for name, data in stored.members.items()},
    }

    assert {where: spelled for where, spelled in found.items() if spelled} == {}


def test_every_member_name_comes_from_the_allow_list_and_none_names_the_environment(
    stored: Stored,
) -> None:
    assert all(ARCHIVE_MEMBER.fullmatch(name) for name in stored.members)
    assert not any(part in name for name in stored.members for part in (".env", "environ", "proc/"))
    assert list(stored.members)[:2] == ["manifest.json", "database.sqlite3"]


async def test_no_log_line_of_a_production_holds_a_key(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    workdir = paths.new_work_directory(world.data_dir)

    await producer.produce_archive(world.database, world.vault, workdir, clock=clock)

    assert leaked(emitted(caplog.records), *KEYS) == []


@pytest.mark.parametrize(
    "planted",
    [
        base64.b64encode(SECOND_KEY).decode(),
        SECOND_KEY.hex(),
        base64.urlsafe_b64encode(FIRST_KEY).decode(),
        FIRST_KEY.hex().upper(),
    ],
    ids=["current-base64", "current-hex", "previous-urlsafe", "previous-upper-hex"],
)
async def test_a_key_planted_in_a_stack_file_stops_the_backup_and_leaves_no_file(
    tmp_path: Path, planted: str, caplog: pytest.LogCaptureFixture
) -> None:
    world = await make_world(tmp_path)
    world.run(
        "UPDATE stack SET compose_file = ? WHERE project_directory = ?",
        (f"# {planted}\n", NEW_STACK),
    )
    workdir = paths.new_work_directory(world.data_dir)

    try:
        with pytest.raises(ProductionFailed) as raised:
            await producer.produce_archive(world.database, world.vault, workdir, clock=clock)
    finally:
        await world.database.aclose()

    assert raised.value.reason == "key_material_found"
    assert list(workdir.iterdir()) == []
    assert planted not in str(raised.value) + emitted(caplog.records)
