"""RewriteStream: the Engine's tar of a volume rewritten into the export archive."""

import asyncio
import gzip
import io
import json
import random
import tarfile
from collections.abc import AsyncIterator

import pytest

from app.docker_engine.errors import EnvironmentUnreachable
from app.services.volume_export_writer import (
    EXTENSION_MAX_BYTES,
    SKIPPED_LIST_MAX,
    RewriteStream,
    StreamMalformed,
)
from tests.archive_support import Member, build_tar
from tests.volume_export_support import (
    LINK_TARGET,
    MIB,
    NOTE,
    NOTE_FILE,
    ROOT,
    SECRET_FILE,
    SECRET_MODE,
    SECRET_OWNER,
    chunked,
    unpack,
    unterminated,
    volume_tar,
)

MANIFEST = b'{"format_version": 1}'
TINY_CHUNK = 700
RANDOM_BLOCK_COUNT = 400
RANDOM_BLOCK_BYTES = 8192


async def rewrite(source: AsyncIterator[bytes]) -> bytes:
    stream = RewriteStream(source, MANIFEST, root=ROOT)
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


def fixture_tar() -> bytes:
    return volume_tar(
        {NOTE_FILE: NOTE, SECRET_FILE: b"s" * 3000},
        Member("link", type=tarfile.SYMTYPE, linkname=LINK_TARGET, mode=0o777),
        Member("hard", type=tarfile.LNKTYPE, linkname=f"{ROOT}/{NOTE_FILE}"),
        Member("pipe", type=tarfile.FIFOTYPE),
        Member("null", type=tarfile.CHRTYPE),
        Member("disk", type=tarfile.BLKTYPE),
    )


async def test_the_archive_opens_with_the_manifest_then_the_files_under_data() -> None:
    archive = await rewrite(chunked(fixture_tar(), TINY_CHUNK))

    members = unpack(archive)

    assert list(members)[:2] == ["manifest.json", "data"]
    assert members["manifest.json"][1] == MANIFEST
    assert members["data"][0].isdir()
    assert members[f"data/{NOTE_FILE}"][1] == NOTE
    assert members[f"data/{SECRET_FILE}"][1] == b"s" * 3000


async def test_symlinks_are_kept_as_written_and_hard_links_follow_their_target() -> None:
    members = unpack(await rewrite(chunked(fixture_tar())))

    link, hard = members["data/link"][0], members["data/hard"][0]

    assert (link.type, link.linkname) == (tarfile.SYMTYPE, LINK_TARGET)
    assert (hard.type, hard.linkname) == (tarfile.LNKTYPE, f"data/{NOTE_FILE}")


async def test_modes_numeric_owners_and_long_names_survive() -> None:
    long_name = "d" * 80 + "/" + "n" * 150
    owned = build_tar(
        Member(f"{ROOT}/", type=tarfile.DIRTYPE, mode=0o755),
        Member(f"{ROOT}/{SECRET_FILE}", b"x", mode=SECRET_MODE),
        Member(f"{ROOT}/{long_name}", b"long"),
    )
    stamped = _with_owner(owned, SECRET_FILE, SECRET_OWNER)

    members = unpack(await rewrite(chunked(stamped)))

    secret = members[f"data/{SECRET_FILE}"][0]
    assert (secret.mode, secret.uid, secret.gid) == (SECRET_MODE, SECRET_OWNER, SECRET_OWNER)
    assert members[f"data/{long_name}"][1] == b"long"


def _with_owner(archive: bytes, name: str, owner: int) -> bytes:
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive)) as source,
        tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as target,
    ):
        for member in source:
            if member.name.endswith(name):
                member.uid = member.gid = owner
            target.addfile(member, source.extractfile(member) if member.isreg() else None)
    return out.getvalue()


async def test_devices_and_pipes_are_left_out_and_listed_in_the_trailing_document() -> None:
    stream = RewriteStream(chunked(fixture_tar()), MANIFEST, root=ROOT)
    archive = b"".join([chunk async for chunk in stream])
    await stream.aclose()

    members = unpack(archive)
    skipped = json.loads(members["skipped.json"][1])

    assert list(members)[-1] == "skipped.json"
    assert not {"data/pipe", "data/null", "data/disk"} & members.keys()
    assert skipped == {
        "count": 3,
        "format_version": 1,
        "skipped": [
            {"path": "pipe", "type": "fifo"},
            {"path": "null", "type": "character_device"},
            {"path": "disk", "type": "block_device"},
        ],
        "truncated": False,
    }
    assert stream.skipped_count == 3


async def test_the_skipped_list_is_capped_but_the_count_is_not() -> None:
    pipes = [Member(f"p{number}", type=tarfile.FIFOTYPE) for number in range(SKIPPED_LIST_MAX + 5)]
    stream = RewriteStream(chunked(volume_tar({}, *pipes)), MANIFEST, root=ROOT)

    archive = b"".join([chunk async for chunk in stream])
    await stream.aclose()

    skipped = json.loads(unpack(archive)["skipped.json"][1])
    assert (skipped["count"], len(skipped["skipped"]), skipped["truncated"]) == (
        SKIPPED_LIST_MAX + 5,
        SKIPPED_LIST_MAX,
        True,
    )


async def test_the_output_is_one_gzip_stream() -> None:
    archive = await rewrite(chunked(fixture_tar()))

    assert archive[:2] == b"\x1f\x8b"
    assert gzip.decompress(archive)[:1] != b""


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(unterminated(volume_tar()), id="no-end-marker"),
        pytest.param(volume_tar() + b"junk", id="data-after-the-end"),
        pytest.param(volume_tar()[:600], id="cut-inside-a-member"),
        pytest.param(b"x" * 512, id="not-a-tar"),
        pytest.param(
            build_tar(Member("elsewhere/", type=tarfile.DIRTYPE)), id="wrong-top-directory"
        ),
        pytest.param(
            build_tar(
                Member(f"{ROOT}/", type=tarfile.DIRTYPE),
                Member(f"{ROOT}/../escape", b"x"),
            ),
            id="parent-segment",
        ),
        pytest.param(
            build_tar(
                Member(f"{ROOT}/", type=tarfile.DIRTYPE),
                Member("other/file", b"x"),
            ),
            id="member-outside-the-directory",
        ),
        pytest.param(
            build_tar(
                Member(f"{ROOT}/", type=tarfile.DIRTYPE),
                Member(f"{ROOT}/h", type=tarfile.LNKTYPE, linkname="elsewhere/x"),
            ),
            id="hard-link-outside-the-directory",
        ),
    ],
)
async def test_a_stream_that_is_not_the_complete_tar_of_one_directory_is_refused(
    source: bytes,
) -> None:
    with pytest.raises(StreamMalformed):
        await rewrite(chunked(source))


async def test_a_header_larger_than_the_limit_is_refused_without_being_read() -> None:
    oversize = tarfile.TarInfo("x")
    oversize.type = tarfile.XHDTYPE
    oversize.size = EXTENSION_MAX_BYTES + 1
    pulled: list[int] = []

    async def source() -> AsyncIterator[bytes]:
        yield volume_tar()[:512]
        yield oversize.tobuf(tarfile.GNU_FORMAT)
        while True:
            pulled.append(1)
            yield b"p" * 65536

    with pytest.raises(StreamMalformed):
        await rewrite(source())

    assert len(pulled) <= 1


async def test_an_error_of_the_engine_stream_comes_through_as_it_was_raised() -> None:
    async def source() -> AsyncIterator[bytes]:
        yield volume_tar()[:1500]
        raise EnvironmentUnreachable(1, "connection lost")

    with pytest.raises(EnvironmentUnreachable):
        await rewrite(source())


async def test_a_failed_archive_ends_without_a_gzip_trailer() -> None:
    received: list[bytes] = []
    big = random.Random(3).randbytes(2 * MIB)
    stream = RewriteStream(chunked(unterminated(volume_tar({"big": big}))), MANIFEST, root=ROOT)

    with pytest.raises(StreamMalformed):
        async for chunk in stream:
            received.append(chunk)
    await stream.aclose()

    assert received
    with pytest.raises(EOFError):
        gzip.decompress(b"".join(received))


async def test_the_source_is_read_only_as_fast_as_the_output_is_taken() -> None:
    blocks = random.Random(1).randbytes(RANDOM_BLOCK_BYTES * RANDOM_BLOCK_COUNT)
    pulled = 0

    async def source() -> AsyncIterator[bytes]:
        nonlocal pulled
        async for chunk in chunked(volume_tar({"big": blocks}), RANDOM_BLOCK_BYTES):
            pulled += 1
            yield chunk

    stream = RewriteStream(source(), MANIFEST, root=ROOT)
    await stream.__anext__()
    await stream.aclose()

    assert len(blocks) > MIB and pulled < RANDOM_BLOCK_COUNT // 2


async def test_closing_an_unfinished_stream_stops_the_worker_and_is_repeatable() -> None:
    stream = RewriteStream(chunked(volume_tar({"big": b"z" * MIB})), MANIFEST, root=ROOT)
    first = await stream.__anext__()

    await stream.aclose()
    await stream.aclose()

    assert first and stream.bytes_out == len(first)
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_a_stream_that_was_never_read_closes_without_starting_a_worker() -> None:
    stream = RewriteStream(chunked(volume_tar()), MANIFEST, root=ROOT)

    await asyncio.wait_for(stream.aclose(), timeout=1)


async def test_output_chunks_stay_far_smaller_than_the_member_that_produced_them() -> None:
    big = random.Random(2).randbytes(3 * MIB)
    stream = RewriteStream(chunked(volume_tar({"big": big})), MANIFEST, root=ROOT)

    sizes = [len(chunk) async for chunk in stream]
    await stream.aclose()

    assert max(sizes) <= MIB and sum(sizes) > 3 * MIB
