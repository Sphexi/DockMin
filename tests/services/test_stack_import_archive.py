"""Validation of a stack export archive: documents, nested volumes and the tar rules."""

import gzip
import io
import json
import tarfile
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from app.services.stack_import_archive import (
    StackArchiveRefused,
    StackLimits,
    VolumeFeed,
    inspect_stack_archive,
    read_contexts,
    stack_volume_tar,
)
from tests.export_archive_support import FIXTURE
from tests.stack_import_support import DATA_VOLUME, NOTE, NOTE_FILE, build_archive

Members = list[tuple[tarfile.TarInfo, bytes]]


def unpacked(archive: bytes) -> Members:
    members: Members = []
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as tar:
        for member in tar:
            source = tar.extractfile(member) if member.isreg() else None
            members.append((member, source.read() if source is not None else b""))
    return members


def packed(members: Members) -> bytes:
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as out,
    ):
        for info, data in members:
            out.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buffer.getvalue()


def entry(
    name: str, kind: bytes = tarfile.REGTYPE, data: bytes = b"", link: str = ""
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, link, 0o644
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data


def before_skipped(members: Members, *added: tuple[tarfile.TarInfo, bytes]) -> Members:
    index = next(i for i, (info, _) in enumerate(members) if info.name.endswith("skipped.json"))
    return [*members[:index], *added, *members[index:]]


def stored(tmp_path: Path, archive: bytes) -> Path:
    path = tmp_path / "upload.tar.gz"
    path.write_bytes(archive)
    return path


def edited(archive: bytes, change: Callable[[Members], Members]) -> bytes:
    return packed(change(unpacked(archive)))


async def test_a_valid_archive_is_summarised_with_its_documents_and_volumes(
    tmp_path: Path,
) -> None:
    archive = await build_archive()

    summary = inspect_stack_archive(stored(tmp_path, archive))

    assert summary.manifest.name == "shop" and summary.compose_text.startswith("services:")
    assert set(summary.files) == {"nginx.conf"}
    [volume] = summary.volumes
    assert (volume.prefix, volume.manifest.name) == ("volumes/0", DATA_VOLUME)
    assert (volume.summary.files, volume.summary.bytes) == (1, len(NOTE))
    assert summary.data_bytes == len(NOTE)


async def test_a_volume_streams_as_a_tar_rooted_at_the_volume(tmp_path: Path) -> None:
    path = stored(tmp_path, await build_archive())

    data = b"".join(stack_volume_tar(path, "volumes/0"))

    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = [member.name for member in tar]
        assert (
            names[-1] == NOTE_FILE and (tar.extractfile(NOTE_FILE) or io.BytesIO()).read() == NOTE
        )


THREE_VOLUMES = {
    "shop_a": {"a.txt": b"a"},
    "shop_b": {"b.txt": b"b"},
    "shop_c": {"c.txt": b"c"},
}


def names_of(chunks: Iterable[bytes]) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as tar:
        return [member.name for member in tar]


async def test_a_feed_serves_volumes_in_order_from_one_read_and_can_skip_one(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, await build_archive(volumes=THREE_VOLUMES))
    feed = VolumeFeed(path)

    first = names_of(feed.tar("volumes/0"))
    third = names_of(feed.tar("volumes/2"))
    feed.close()

    assert first == ["a.txt"] and third == ["c.txt"]


async def test_a_feed_refuses_a_volume_out_of_order_or_asked_for_twice(tmp_path: Path) -> None:
    path = stored(tmp_path, await build_archive(volumes=THREE_VOLUMES))
    feed = VolumeFeed(path)
    names_of(feed.tar("volumes/1"))

    with pytest.raises(StackArchiveRefused):
        feed.tar("volumes/0")
    with pytest.raises(StackArchiveRefused):
        feed.tar("volumes/1")
    feed.close()


async def test_a_single_volume_stops_at_the_next_one_and_ignores_earlier_ones(
    tmp_path: Path,
) -> None:
    path = stored(tmp_path, await build_archive(volumes=THREE_VOLUMES))

    assert names_of(stack_volume_tar(path, "volumes/1")) == ["b.txt"]


async def test_the_fixture_archive_reads(tmp_path: Path) -> None:
    summary = inspect_stack_archive(FIXTURE)

    assert summary.manifest.format_version == 1
    assert read_contexts(FIXTURE, summary.manifest) == {}


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (entry("volumes/0/data/dev", tarfile.CHRTYPE), "not a file, directory or link"),
        (entry("volumes/0/data/fifo", tarfile.FIFOTYPE), "not a file, directory or link"),
        (entry("volumes/0/data//abs"), "unusable name"),
        (entry("volumes/0/data/a/../b"), "contains `..`"),
        (entry("volumes/0/data/out", tarfile.SYMTYPE, link="../../escape"), "leaves the volume"),
        (entry("volumes/0/data/notes/a.txt"), "same name twice"),
        (entry("volumes/0/outside"), "outside its data directory"),
        (entry("volumes/0/data/hard", tarfile.LNKTYPE, link="volumes/0/data/later"), "hard link"),
    ],
)
async def test_a_nested_member_that_breaks_a_tar_rule_is_refused_naming_it(
    tmp_path: Path, bad: tuple[tarfile.TarInfo, bytes], message: str
) -> None:
    archive = edited(await build_archive(), lambda members: before_skipped(members, bad))

    with pytest.raises(StackArchiveRefused) as refused:
        inspect_stack_archive(stored(tmp_path, archive))

    assert message in refused.value.message and refused.value.message.startswith("Volume 0:")


async def test_a_member_below_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    archive = edited(
        await build_archive(),
        lambda members: before_skipped(
            members,
            entry("volumes/0/data/etc", tarfile.SYMTYPE, link="/etc"),
            entry("volumes/0/data/etc/passwd"),
        ),
    )

    with pytest.raises(StackArchiveRefused) as refused:
        inspect_stack_archive(stored(tmp_path, archive))

    assert "below a symbolic link" in refused.value.message
    assert refused.value.member == "data/etc/passwd"


async def test_an_archive_that_is_not_an_export_is_refused(tmp_path: Path) -> None:
    plain = packed([entry("a.txt", data=b"x")])

    with pytest.raises(StackArchiveRefused) as refused:
        inspect_stack_archive(stored(tmp_path, plain))
    with pytest.raises(StackArchiveRefused) as not_gzip:
        inspect_stack_archive(stored(tmp_path, b"plain text"))

    assert "does not start with manifest.json" in refused.value.message
    assert "not a gzip archive" in not_gzip.value.message


async def test_a_newer_format_version_is_refused_naming_it(tmp_path: Path) -> None:
    def newer(members: Members) -> Members:
        info, data = members[0]
        document = json.loads(data)
        document["format_version"] = 2
        raw = json.dumps(document).encode()
        info.size = len(raw)
        return [(info, raw), *members[1:]]

    archive = edited(await build_archive(), newer)

    with pytest.raises(StackArchiveRefused) as refused:
        inspect_stack_archive(stored(tmp_path, archive))

    assert refused.value.version == 2 and "version is 2" in refused.value.message


async def test_a_document_that_differs_from_its_manifest_is_refused(tmp_path: Path) -> None:
    def altered(members: Members) -> Members:
        changed: Members = []
        for info, data in members:
            if info.name == "stack/compose.yaml":
                data = data.replace(b"nginx", b"nginy")
            changed.append((info, data))
        return changed

    with pytest.raises(StackArchiveRefused) as refused:
        inspect_stack_archive(stored(tmp_path, edited(await build_archive(), altered)))

    assert "do not match its manifest" in refused.value.message


async def test_an_undeclared_document_or_a_missing_volume_is_refused(tmp_path: Path) -> None:
    extra = edited(
        await build_archive(),
        lambda members: [*members[:2], entry("stack/extra.txt", data=b"x"), *members[2:]],
    )
    missing = edited(
        await build_archive(),
        lambda members: [m for m in members if not m[0].name.startswith("volumes/")],
    )

    for archive in (extra, missing):
        with pytest.raises(StackArchiveRefused) as refused:
            inspect_stack_archive(stored(tmp_path, archive))
        assert "do not match its manifest" in refused.value.message


async def test_a_truncated_archive_is_refused(tmp_path: Path) -> None:
    archive = await build_archive()

    with pytest.raises(StackArchiveRefused):
        inspect_stack_archive(stored(tmp_path, archive[: len(archive) // 2]))


async def test_the_member_and_byte_limits_apply_across_every_volume(tmp_path: Path) -> None:
    archive = await build_archive(
        text="services:\n  a:\n    image: nginx:1\n",
        volumes={"shop_data": {"a": b"1", "b": b"2"}, "other": {"c": b"3"}},
    )
    path = stored(tmp_path, archive)

    with pytest.raises(StackArchiveRefused) as members:
        inspect_stack_archive(path, StackLimits(max_members=2))
    with pytest.raises(StackArchiveRefused) as size:
        inspect_stack_archive(path, StackLimits(max_bytes=2))

    assert "more entries" in members.value.message and "size limit" in size.value.message
