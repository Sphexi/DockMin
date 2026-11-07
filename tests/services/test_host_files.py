"""Tests for HostFileReader: the helper container's body and life, and the per-path results."""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.docker_engine import EngineClient
from app.docker_engine.errors import EngineError
from app.services.host_files import (
    HELPER_ROLE,
    HELPER_ROLE_LABEL,
    LEFTOVER_AGE_SECONDS,
    MOUNT_POINT,
    PATH_MAX_BYTES,
    FileRefusal,
    FileRefusalReason,
    HelperMountRefused,
    HostFile,
    HostFileReader,
    InvalidWorkingDirectory,
    WorkingDirectoryProblem,
    check_working_directory,
    is_relative_path,
)
from tests.archive_support import (
    GO_MODE_DIR,
    GO_MODE_SYMLINK,
    Member,
    build_tar,
    single_file_tar,
    stat_header,
)
from tests.docker_engine.container_engine import CREATED, ContainerEngine
from tests.docker_engine.fake_engine import Reply, Scope, error_reply, split_version
from tests.docker_engine.helpers import ENV_ID

ADMIN = Principal(1, "admin", "administrator")
STANDARD = Principal(2, "standard", "standard")
IMAGE = "busybox:1"
WORKING_DIR = "/srv/stacks/shop"
LIMIT = 1024
CONTENT = b"services: {}\n"
OLD = CREATED + LEFTOVER_AGE_SECONDS + 1
YOUNG = CREATED + LEFTOVER_AGE_SECONDS - 1
HELPER_NAME = re.compile(r"dockmin-read-[0-9a-f]{12}")
STALE_ID = "5" * 64
STALE_NAME = "dockmin-read-0123456789ab"
HELPER_LABELS = {HELPER_ROLE_LABEL: HELPER_ROLE}


class ReaderEngine(ContainerEngine):
    """A daemon that serves the archive endpoint for the containers it holds."""

    def __init__(self) -> None:
        super().__init__()
        self.served: dict[str, Reply] = {}
        self.archive_reads: list[tuple[str, str]] = []

    def serve(self, relative_path: str, content: bytes) -> None:
        name = relative_path.rsplit("/", 1)[-1]
        self.served[f"{MOUNT_POINT}/{relative_path}"] = Reply(
            200, single_file_tar(name, content), stat_header(name, len(content))
        )

    async def _reply(self, scope: Scope, body: bytes) -> Reply | None:
        _, path = split_version(scope["path"])
        parts = path.strip("/").split("/")
        if scope["method"] != "GET" or parts[0] != "containers" or parts[2:] != ["archive"]:
            return await super()._reply(scope, body)
        query = {k: v[-1] for k, v in parse_qs(scope["query_string"].decode()).items()}
        box = self.resolve(parts[1])
        if box is None:
            return error_reply(404)
        self.archive_reads.append((box.state, query["path"]))
        await self._pass_gate("archive")
        failed = self._failure("archive", box.id)
        if failed is not None:
            return failed
        return self.served.get(query["path"], error_reply(404))


@pytest.fixture
def daemon() -> ReaderEngine:
    engine = ReaderEngine()
    engine.add_image(IMAGE)
    return engine


@pytest.fixture
async def client(daemon: ReaderEngine) -> AsyncIterator[EngineClient]:
    async with EngineClient(ENV_ID, daemon.bundle()) as opened:
        yield opened
    daemon.release_gates()


@pytest.fixture
def reader(client: EngineClient) -> HostFileReader:
    return HostFileReader(client, clock=lambda: OLD)


def leave_helper(daemon: ReaderEngine, container_id: str, name: str, state: str) -> None:
    daemon.add(container_id, name, state=state, image=IMAGE, labels=dict(HELPER_LABELS))


async def read(reader: HostFileReader, *paths: str) -> Any:
    return await reader.read(ADMIN, WORKING_DIR, paths, image=IMAGE, limit=LIMIT)


async def test_each_path_comes_back_as_its_bytes_or_its_own_refusal(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.serve("compose.yaml", CONTENT)
    daemon.served[f"{MOUNT_POINT}/link"] = Reply(
        200, b"", stat_header("link", 0, GO_MODE_SYMLINK | 0o777)
    )
    daemon.served[f"{MOUNT_POINT}/dir"] = Reply(
        200, b"", stat_header("dir", 0, GO_MODE_DIR | 0o755)
    )
    daemon.served[f"{MOUNT_POINT}/big"] = Reply(200, b"", stat_header("big", LIMIT + 1))
    daemon.served[f"{MOUNT_POINT}/two"] = Reply(
        200, build_tar(Member("two", CONTENT), Member("more", CONTENT)), stat_header("two", 1)
    )
    daemon.served[f"{MOUNT_POINT}/named"] = Reply(
        200, single_file_tar("other", CONTENT), stat_header("named", len(CONTENT))
    )

    result = await read(
        reader, "compose.yaml", "link", "dir", "big", "two", "named", "gone", "../x", "/abs"
    )

    assert result.results == {
        "compose.yaml": HostFile("compose.yaml", CONTENT),
        "link": FileRefusal("link", FileRefusalReason.SYMLINK),
        "dir": FileRefusal("dir", FileRefusalReason.NOT_REGULAR_FILE),
        "big": FileRefusal("big", FileRefusalReason.TOO_LARGE),
        "two": FileRefusal("two", FileRefusalReason.MEMBER_COUNT),
        "named": FileRefusal("named", FileRefusalReason.MEMBER_NAME),
        "gone": FileRefusal("gone", FileRefusalReason.MISSING),
        "../x": FileRefusal("../x", FileRefusalReason.INVALID_PATH),
        "/abs": FileRefusal("/abs", FileRefusalReason.INVALID_PATH),
    }
    assert result.refused["gone"] is FileRefusalReason.MISSING
    assert [path for _, path in daemon.archive_reads] == [
        f"{MOUNT_POINT}/{name}"
        for name in ("compose.yaml", "link", "dir", "big", "two", "named", "gone")
    ]


async def test_the_helper_is_created_with_the_documented_body_never_started_and_removed(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.serve("compose.yaml", CONTENT)
    daemon.serve("sub/.env", b"A=1\n")

    result = await read(reader, "compose.yaml", "sub/.env")

    request = daemon.calls("POST", "/containers/create")[0]
    removal = daemon.calls("DELETE", f"/containers/{result.helper_container}")[0]
    assert daemon.bodies() == [
        {
            "Image": IMAGE,
            "Labels": {"dockmin.role": "file-reader"},
            "NetworkDisabled": True,
            "HostConfig": {
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": WORKING_DIR,
                        "Target": "/dockmin-read",
                        "ReadOnly": True,
                    }
                ],
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "AutoRemove": False,
            },
        }
    ]
    assert HELPER_NAME.fullmatch(request.query["name"])
    assert daemon.verbs() == ["create", "remove"]
    assert removal.query == {"force": "false", "v": "false"}
    assert [state for state, _ in daemon.archive_reads] == ["created", "created"]
    assert daemon.boxes == {}
    assert result.helper_removed is True


async def test_one_helper_serves_several_reads_with_their_own_limits(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.serve("compose.yaml", CONTENT)
    daemon.serve("extra.env", CONTENT)

    async with reader.session(ADMIN, WORKING_DIR, image=IMAGE) as session:
        first = await session.read("compose.yaml", limit=LIMIT)
        second = await session.read("extra.env", limit=len(CONTENT) - 1)

    assert first == HostFile("compose.yaml", CONTENT)
    assert second == FileRefusal("extra.env", FileRefusalReason.TOO_LARGE)
    assert daemon.verbs() == ["create", "remove"]


async def test_reading_after_the_session_ended_is_refused(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    async with reader.session(ADMIN, WORKING_DIR, image=IMAGE) as session:
        pass

    with pytest.raises(RuntimeError):
        await session.read("compose.yaml", limit=LIMIT)


async def test_the_helper_is_removed_when_a_read_fails_and_the_failure_propagates(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.fail("archive", 500)

    with pytest.raises(EngineError):
        await read(reader, "compose.yaml")

    assert daemon.verbs() == ["create", "remove"]
    assert daemon.boxes == {}


async def test_the_helper_is_removed_when_the_read_is_cancelled(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    gate = daemon.hold("archive")
    task = asyncio.create_task(read(reader, "compose.yaml"))
    await asyncio.wait_for(gate.entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon.verbs() == ["create", "remove"]
    assert daemon.boxes == {}


async def test_a_removal_the_host_refuses_is_reported_without_losing_the_read(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.serve("compose.yaml", CONTENT)
    daemon.fail("remove", 500)

    result = await read(reader, "compose.yaml")

    assert result.results == {"compose.yaml": HostFile("compose.yaml", CONTENT)}
    assert result.helper_removed is False
    assert list(daemon.boxes) == [result.helper_container]


async def test_a_create_the_host_refuses_leaves_nothing_to_remove(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.fail("create", 500)

    with pytest.raises(EngineError):
        await read(reader, "compose.yaml")

    assert daemon.verbs() == ["create"]


async def test_a_create_the_engine_refuses_as_a_bad_request_is_a_mount_refusal(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    daemon.fail("create", 400)

    with pytest.raises(HelperMountRefused):
        await read(reader, "compose.yaml")

    assert daemon.verbs() == ["create"]


async def test_a_helper_whose_create_was_cancelled_in_flight_is_removed_once_it_returns(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    gate = daemon.hold("create")
    task = asyncio.create_task(read(reader, "compose.yaml"))
    await asyncio.wait_for(gate.entered.wait(), timeout=5)

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon.verbs() == ["create", "remove"]
    assert daemon.boxes == {}


async def test_a_standard_user_is_refused_before_any_engine_request(
    daemon: ReaderEngine, client: EngineClient
) -> None:
    reader = HostFileReader(client)

    with pytest.raises(PermissionDenied):
        await reader.read(STANDARD, WORKING_DIR, ["compose.yaml"], image=IMAGE, limit=LIMIT)

    assert daemon.requests == []


async def test_a_working_directory_that_cannot_be_mounted_is_refused_before_any_request(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    with pytest.raises(InvalidWorkingDirectory):
        await reader.read(ADMIN, "/srv/a:/etc", ["compose.yaml"], image=IMAGE, limit=LIMIT)

    assert daemon.requests == []


@pytest.mark.parametrize(
    ("age", "removed"), [(LEFTOVER_AGE_SECONDS + 1, True), (LEFTOVER_AGE_SECONDS, False)]
)
async def test_a_created_helper_is_removed_only_once_older_than_ten_minutes(
    daemon: ReaderEngine, client: EngineClient, age: int, removed: bool
) -> None:
    leave_helper(daemon, STALE_ID, STALE_NAME, "created")
    reader = HostFileReader(client, clock=lambda: CREATED + age)

    found = await reader.remove_leftovers()

    assert [(h.container_id, h.removed) for h in found] == [(STALE_ID, removed)]
    assert (STALE_ID in daemon.boxes) is not removed


async def test_helpers_in_any_other_state_or_under_another_name_are_reported_and_left(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    leave_helper(daemon, "1" * 64, "dockmin-read-aaaaaaaaaaaa", "running")
    leave_helper(daemon, "2" * 64, "dockmin-read-bbbbbbbbbbbb", "exited")
    leave_helper(daemon, "3" * 64, "dockmin-read-cccccccccccc", "paused")
    leave_helper(daemon, "4" * 64, "someone-elses-container", "created")
    daemon.add("6" * 64, "unlabelled", state="created", image=IMAGE)
    leave_helper(daemon, STALE_ID, STALE_NAME, "created")

    found = await reader.remove_leftovers()

    assert {(h.state, h.removed) for h in found} == {
        ("running", False),
        ("exited", False),
        ("paused", False),
        ("created", False),
        ("created", True),
    }
    assert sorted(daemon.boxes) == ["1" * 64, "2" * 64, "3" * 64, "4" * 64, "6" * 64]
    assert daemon.verbs() == ["remove"]


async def test_a_session_sweeps_leftovers_before_creating_and_reports_them(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    leave_helper(daemon, STALE_ID, STALE_NAME, "created")
    daemon.serve("compose.yaml", CONTENT)

    result = await read(reader, "compose.yaml")

    assert [(h.container_id, h.removed) for h in result.leftovers] == [(STALE_ID, True)]
    assert daemon.verbs() == ["remove", "create", "remove"]


async def test_a_removal_that_fails_during_the_sweep_is_reported_as_left(
    daemon: ReaderEngine, reader: HostFileReader
) -> None:
    leave_helper(daemon, STALE_ID, STALE_NAME, "created")
    daemon.fail("remove", 500, target=STALE_ID)

    found = await reader.remove_leftovers()

    assert [(h.container_id, h.removed) for h in found] == [(STALE_ID, False)]


@pytest.mark.parametrize(
    ("path", "problem"),
    [
        pytest.param("srv/app", WorkingDirectoryProblem.NOT_ABSOLUTE, id="relative"),
        pytest.param("/", WorkingDirectoryProblem.ROOT, id="root"),
        pytest.param("/srv/../etc", WorkingDirectoryProblem.NOT_NORMALISED, id="parent"),
        pytest.param("/srv/./app", WorkingDirectoryProblem.NOT_NORMALISED, id="current"),
        pytest.param("/srv//app", WorkingDirectoryProblem.NOT_NORMALISED, id="double-slash"),
        pytest.param("/srv/app/", WorkingDirectoryProblem.NOT_NORMALISED, id="trailing-slash"),
        pytest.param("/srv/a:/etc", WorkingDirectoryProblem.BIND_SEPARATOR, id="bind-separator"),
        pytest.param("/srv/a\nb", WorkingDirectoryProblem.CONTROL_CHARACTERS, id="newline"),
        pytest.param("/srv/a\x7fb", WorkingDirectoryProblem.CONTROL_CHARACTERS, id="delete"),
        pytest.param("/srv/\udce9", WorkingDirectoryProblem.CONTROL_CHARACTERS, id="unencodable"),
        pytest.param("/" + "a" * PATH_MAX_BYTES, WorkingDirectoryProblem.TOO_LONG, id="too-long"),
    ],
)
def test_a_working_directory_that_is_not_absolute_and_plain_is_refused(
    path: str, problem: WorkingDirectoryProblem
) -> None:
    with pytest.raises(InvalidWorkingDirectory) as caught:
        check_working_directory(path)

    assert caught.value.problem is problem


def test_a_plain_absolute_working_directory_is_returned_unchanged() -> None:
    longest = "/" + "a" * (PATH_MAX_BYTES - 1)

    assert check_working_directory(WORKING_DIR) == WORKING_DIR
    assert check_working_directory(longest) == longest


@pytest.mark.parametrize(
    ("path", "accepted"),
    [
        ("compose.yaml", True),
        ("sub/dir/.env", True),
        ("", False),
        ("/abs", False),
        ("../x", False),
        ("a/../b", False),
        ("a/./b", False),
        ("a//b", False),
        ("a/", False),
        ("a\nb", False),
        ("\udce9", False),
        ("a" * (PATH_MAX_BYTES + 1), False),
    ],
)
def test_a_relative_path_must_be_plain_and_below_the_working_directory(
    path: str, accepted: bool
) -> None:
    assert is_relative_path(path) is accepted
