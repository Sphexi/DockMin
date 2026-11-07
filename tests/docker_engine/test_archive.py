"""Tests for ContainersApi.archive: a regular file read through the Engine archive endpoint."""

import asyncio
import tarfile
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import pytest

import app.docker_engine.archive as archive
from app.docker_engine import EngineClient
from app.docker_engine.archive import (
    TAR_OVERHEAD_BYTES,
    ArchiveEntry,
    ArchiveMalformed,
    FileMissing,
    FileTooLarge,
    InvalidArchivePath,
    MemberCountMismatch,
    MemberNameMismatch,
    NotARegularFile,
    SymlinkRefused,
)
from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, PermissionDenied
from tests.archive_support import (
    FILE_MODE,
    GO_MODE_DEVICE,
    GO_MODE_DIR,
    GO_MODE_SYMLINK,
    HEADER,
    Member,
    build_tar,
    encode_stat,
    single_file_tar,
    stat_header,
)
from tests.docker_engine.fake_engine import FakeEngine, Reply, error_reply

CONTAINER = "ab" * 32
ARCHIVE_PATH = f"/containers/{CONTAINER}/archive"
FILE_PATH = "/dockmin-read/sub/compose.yaml"
NAME = "compose.yaml"
LIMIT = 1024
CONTENT = b"services: {}\n"


def serve(engine: FakeEngine, body: bytes, headers: Mapping[str, str] | None = None) -> None:
    engine.route(
        "GET",
        ARCHIVE_PATH,
        Reply(200, body, stat_header(NAME, len(CONTENT)) if headers is None else headers),
    )


async def read(client: EngineClient, path: str = FILE_PATH, limit: int = LIMIT) -> ArchiveEntry:
    return await client.containers.archive(CONTAINER, path, limit=limit)


async def test_a_regular_file_is_returned_with_its_bytes_and_base_name(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    serve(engine, single_file_tar(NAME, CONTENT))

    entry = await read(engine_client)

    request = engine.calls("GET", ARCHIVE_PATH)[0]
    assert entry == ArchiveEntry(NAME, CONTENT)
    assert request.query == {"path": FILE_PATH}


async def test_a_file_of_exactly_the_limit_is_accepted(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    content = b"x" * LIMIT
    serve(engine, single_file_tar(NAME, content), stat_header(NAME, LIMIT))

    entry = await read(engine_client)

    assert entry.content == content


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(
            stat_header(NAME, 1, GO_MODE_SYMLINK | 0o777), SymlinkRefused, id="symlink-mode"
        ),
        pytest.param(
            stat_header(NAME, 1, link_target="/etc/passwd"), SymlinkRefused, id="link-target"
        ),
        pytest.param(stat_header("d", 0, GO_MODE_DIR | 0o755), NotARegularFile, id="directory"),
        pytest.param(stat_header("d", 0, GO_MODE_DEVICE | 0o600), NotARegularFile, id="device"),
        pytest.param(stat_header(NAME, LIMIT + 1), FileTooLarge, id="over-limit"),
    ],
)
async def test_a_path_that_is_not_a_small_regular_file_is_refused_before_its_body_is_read(
    engine: FakeEngine,
    engine_client: EngineClient,
    headers: dict[str, str],
    expected: type[EngineError],
) -> None:
    oversize = b"x" * (LIMIT + TAR_OVERHEAD_BYTES + 1)
    serve(engine, oversize, headers)

    with pytest.raises(expected):
        await read(engine_client)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            build_tar(Member(NAME, CONTENT), Member("other", CONTENT)),
            MemberCountMismatch,
            id="two-members",
        ),
        pytest.param(single_file_tar("other.yaml", CONTENT), MemberNameMismatch, id="other-name"),
        pytest.param(single_file_tar(f"sub/{NAME}", CONTENT), MemberNameMismatch, id="path-name"),
        pytest.param(
            build_tar(Member(NAME, b"", type=tarfile.SYMTYPE, linkname="/etc/passwd")),
            SymlinkRefused,
            id="member-is-a-symlink",
        ),
        pytest.param(
            build_tar(Member(NAME, b"", type=tarfile.DIRTYPE)), NotARegularFile, id="member-is-dir"
        ),
        pytest.param(
            single_file_tar(NAME, b"x" * (LIMIT + 1)), FileTooLarge, id="member-over-limit"
        ),
        pytest.param(b"x" * (LIMIT + TAR_OVERHEAD_BYTES + 1), FileTooLarge, id="body-over-cap"),
        pytest.param(build_tar(), ArchiveMalformed, id="no-members"),
        pytest.param(b"not a tar archive" * 64, ArchiveMalformed, id="not-a-tar"),
        pytest.param(single_file_tar(NAME, CONTENT)[:700], ArchiveMalformed, id="truncated"),
        pytest.param(
            build_tar(Member(NAME, CONTENT), tail=b"junk"), ArchiveMalformed, id="data-after-end"
        ),
    ],
)
async def test_a_body_that_is_not_exactly_the_one_requested_file_is_refused(
    engine: FakeEngine, engine_client: EngineClient, body: bytes, expected: type[EngineError]
) -> None:
    serve(engine, body, stat_header(NAME, len(CONTENT)))

    with pytest.raises(expected):
        await read(engine_client)


@pytest.mark.parametrize("status", [404, 400])
async def test_a_missing_path_is_a_missing_file_whichever_status_the_engine_uses(
    engine: FakeEngine, engine_client: EngineClient, status: int
) -> None:
    engine.route("GET", ARCHIVE_PATH, error_reply(status))

    with pytest.raises(FileMissing) as caught:
        await read(engine_client)

    assert caught.value.engine_message is None


async def test_other_engine_failures_are_not_reported_as_file_refusals(
    engine: FakeEngine, engine_client: EngineClient
) -> None:
    engine.route("GET", ARCHIVE_PATH, error_reply(403), error_reply(500))

    with pytest.raises(PermissionDenied):
        await read(engine_client)
    with pytest.raises(EngineError) as caught:
        await read(engine_client)

    assert caught.value.status_code == 500


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="absent"),
        pytest.param({HEADER: ""}, id="empty"),
        pytest.param({HEADER: "***not base64***"}, id="not-base64"),
        pytest.param(
            {HEADER: encode_stat({"name": NAME, "size": 1, "mode": FILE_MODE})[:-6]},
            id="truncated-document",
        ),
        pytest.param({HEADER: encode_stat([1, 2])}, id="not-an-object"),
        pytest.param({HEADER: encode_stat({"name": NAME, "size": 1})}, id="no-mode"),
        pytest.param({HEADER: encode_stat({"name": 7, "size": 1, "mode": 420})}, id="name-type"),
        pytest.param({HEADER: encode_stat({"name": NAME, "size": -1, "mode": 420})}, id="negative"),
        pytest.param({HEADER: encode_stat({"name": NAME, "size": True, "mode": 420})}, id="bool"),
        pytest.param({HEADER: encode_stat({"name": NAME, "size": "1", "mode": 420})}, id="text"),
        pytest.param(
            {HEADER: encode_stat({"name": NAME, "size": 1, "mode": FILE_MODE, "linkTarget": 3})},
            id="link-target-type",
        ),
        pytest.param({HEADER: "A" * (archive.STAT_HEADER_MAX_CHARS + 4)}, id="oversize"),
    ],
)
async def test_a_stat_header_that_is_missing_or_malformed_is_refused(
    engine: FakeEngine, engine_client: EngineClient, headers: dict[str, str]
) -> None:
    serve(engine, single_file_tar(NAME, CONTENT), headers)

    with pytest.raises(ArchiveMalformed):
        await read(engine_client)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("compose.yaml", id="relative"),
        pytest.param("/", id="root"),
        pytest.param("/a/../b", id="parent"),
        pytest.param("/a/./b", id="current"),
        pytest.param("/a//b", id="double-slash"),
        pytest.param("/a/b/", id="trailing-slash"),
        pytest.param("/a/b\nc", id="control-character"),
        pytest.param("/" + "a" * archive.PATH_MAX_BYTES, id="too-long"),
        pytest.param("/a/\udce9", id="not-encodable"),
    ],
)
async def test_a_path_that_is_not_absolute_and_normalised_is_refused_unsent(
    engine: FakeEngine, engine_client: EngineClient, path: str
) -> None:
    with pytest.raises(InvalidArchivePath):
        await read(engine_client, path)

    assert engine.calls("GET", ARCHIVE_PATH) == []


@pytest.mark.parametrize("container_id", ["web", "ab" * 31, "AB" * 32, "ab" * 32 + "/x"])
async def test_a_container_reference_that_is_not_a_full_id_is_refused_unsent(
    engine: FakeEngine, engine_client: EngineClient, container_id: str
) -> None:
    with pytest.raises(EngineError):
        await engine_client.containers.archive(container_id, FILE_PATH, limit=LIMIT)

    assert engine.calls("GET") == []


async def test_a_read_that_runs_past_the_deadline_is_an_unreachable_host(
    engine: FakeEngine, engine_client: EngineClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stalled(
        self: EngineHttp, *args: Any, on_headers: Any = None, **kwargs: Any
    ) -> AsyncGenerator[bytes, None]:
        on_headers(stat_header(NAME, len(CONTENT)))
        await asyncio.sleep(60)
        yield b""

    monkeypatch.setattr(EngineHttp, "stream_bytes", stalled)
    monkeypatch.setattr(archive, "ARCHIVE_SECONDS", 0.05)

    with pytest.raises(EnvironmentUnreachable):
        await read(engine_client)
