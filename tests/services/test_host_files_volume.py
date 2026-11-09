"""HostFileReader.volume_session: a helper that mounts one volume read-only, and its removal."""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.docker_engine import EngineClient
from app.services.host_files import (
    HELPER_ROLE,
    HELPER_ROLE_LABEL,
    LEFTOVER_AGE_SECONDS,
    MAX_IMAGE_INSPECTS,
    HelperImageMissing,
    HelperImageUnusable,
    HelperMountRefused,
    HostFileReader,
    check_helper_image,
    helper_image,
    volume_helper_body,
)
from tests.docker_engine.container_engine import CREATED, IMAGE_ID, anonymous_volume
from tests.docker_engine.helpers import ENV_ID
from tests.volume_export_support import (
    MOUNT_POINT,
    VolumeEngine,
    helper_mount,
    volume_tar,
)

ADMIN = Principal(1, "admin", "administrator")
STANDARD = Principal(2, "standard", "standard")
PLAIN_IMAGE = "busybox:1"
VOLUME_IMAGE = "withvolume:1"
VOLUME_IMAGE_ID = "sha256:" + "d" * 64
PLAIN_IMAGE_ID = "sha256:" + "e" * 64
VOLUME = "data"
OLD = CREATED + LEFTOVER_AGE_SECONDS + 1
HELPER_NAME = re.compile(r"dockmin-read-[0-9a-f]{12}")
STALE_ID = "5" * 64
STALE_NAME = "dockmin-read-0123456789ab"
DECLARED: dict[str, Any] = {"Volumes": {"/declared": {}}}


@pytest.fixture
def daemon() -> VolumeEngine:
    engine = VolumeEngine()
    engine.add_image(PLAIN_IMAGE, PLAIN_IMAGE_ID, size=100)
    engine.add_volume(VOLUME)
    engine.serve_volume(VOLUME, volume_tar())
    return engine


@pytest.fixture
async def client(daemon: VolumeEngine) -> AsyncIterator[EngineClient]:
    async with EngineClient(ENV_ID, daemon.bundle()) as opened:
        yield opened
    daemon.release_gates()


@pytest.fixture
def reader(client: EngineClient) -> HostFileReader:
    return HostFileReader(client, clock=lambda: OLD)


def removals(daemon: VolumeEngine) -> list[dict[str, str]]:
    return [dict(call.query) for call in daemon.calls("DELETE") if "/containers/" in call.path]


def test_a_writable_helper_body_differs_only_in_the_mount_being_read_write() -> None:
    read_only, writable = (
        volume_helper_body("img", VOLUME),
        volume_helper_body("img", VOLUME, writable=True),
    )

    (mount,) = writable["HostConfig"]["Mounts"]
    assert (mount["ReadOnly"], mount["Target"], mount["Source"]) == (False, MOUNT_POINT, VOLUME)
    assert writable["HostConfig"] | {"Mounts": []} == read_only["HostConfig"] | {"Mounts": []}
    assert writable["NetworkDisabled"] is True


def test_the_helper_body_mounts_the_volume_read_only_with_nothing_else_reachable() -> None:
    body = volume_helper_body("img", VOLUME)

    assert body["HostConfig"] == {
        "Mounts": [{"Type": "volume", "Source": VOLUME, "Target": MOUNT_POINT, "ReadOnly": True}],
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "AutoRemove": False,
    }
    assert body["NetworkDisabled"] is True
    assert body["Labels"] == {HELPER_ROLE_LABEL: HELPER_ROLE}


async def test_the_helper_is_created_never_started_and_streams_the_volume(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    async with reader.volume_session(ADMIN, VOLUME, image=PLAIN_IMAGE) as helper:
        (box,) = daemon.helpers()
        stream = await helper.stream()
        received = b"".join([chunk async for chunk in stream])

    assert HELPER_NAME.fullmatch(box.name) and box.image == PLAIN_IMAGE
    assert helper_mount(box)["Name"] == VOLUME and helper_mount(box)["RW"] is False
    assert (box.host_config or {})["NetworkMode"] == "none"
    assert daemon.archive_reads == ["created"] and daemon.verbs() == ["create", "remove"]
    assert received == volume_tar() and helper.removed


async def test_the_helper_and_its_anonymous_volumes_are_removed_on_success(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED)

    async with reader.volume_session(ADMIN, VOLUME, image=VOLUME_IMAGE):
        assert any(daemon.volumes[name] for name in daemon.volumes)

    assert daemon.helpers() == []
    assert removals(daemon) == [{"force": "false", "v": "true"}]
    assert list(daemon.volumes) == [VOLUME]


async def test_the_helper_is_removed_when_the_read_fails(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    with pytest.raises(RuntimeError):
        async with reader.volume_session(ADMIN, VOLUME, image=PLAIN_IMAGE):
            raise RuntimeError

    assert daemon.helpers() == [] and removals(daemon) == [{"force": "false", "v": "true"}]


async def test_the_helper_is_removed_when_the_caller_is_cancelled(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    entered = asyncio.Event()
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED)

    async def work() -> None:
        async with reader.volume_session(ADMIN, VOLUME, image=VOLUME_IMAGE):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.ensure_future(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon.helpers() == [] and list(daemon.volumes) == [VOLUME]


async def test_a_create_cancelled_in_flight_is_removed_with_its_anonymous_volumes(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED)
    gate = daemon.hold("create")

    async def work() -> None:
        async with reader.volume_session(ADMIN, VOLUME, image=VOLUME_IMAGE):
            pytest.fail("the block must not run")

    task = asyncio.ensure_future(work())
    await gate.entered.wait()
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon.helpers() == [] and list(daemon.volumes) == [VOLUME]


async def test_a_refused_mount_and_a_standard_user_create_nothing(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    daemon.fail("create", 400)

    with pytest.raises(HelperMountRefused):
        async with reader.volume_session(ADMIN, VOLUME, image=PLAIN_IMAGE):
            pytest.fail("the block must not run")
    with pytest.raises(PermissionDenied):
        async with reader.volume_session(STANDARD, VOLUME, image=PLAIN_IMAGE):
            pytest.fail("the block must not run")

    assert daemon.helpers() == []


async def test_the_leftover_sweep_removes_a_stale_volume_helper_with_its_anonymous_volumes(
    daemon: VolumeEngine, reader: HostFileReader
) -> None:
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED)
    daemon.add(
        STALE_ID,
        STALE_NAME,
        state="created",
        labels={HELPER_ROLE_LABEL: HELPER_ROLE},
        image=VOLUME_IMAGE,
        mounts=[
            {"Type": "volume", "Name": VOLUME, "Destination": MOUNT_POINT, "RW": False},
            {"Type": "volume", "Name": anonymous_volume(9), "Destination": "/d", "RW": True},
        ],
    )
    daemon.volumes[anonymous_volume(9)] = True

    leftovers = await reader.remove_leftovers()

    assert [(item.container_id, item.removed) for item in leftovers] == [(STALE_ID, True)]
    assert removals(daemon) == [{"force": "false", "v": "true"}]
    assert anonymous_volume(9) not in daemon.volumes and VOLUME in daemon.volumes


async def test_the_sweep_leaves_a_helper_whose_export_is_still_running(
    daemon: VolumeEngine, client: EngineClient, reader: HostFileReader
) -> None:
    async with reader.volume_session(ADMIN, VOLUME, image=PLAIN_IMAGE):
        (running,) = daemon.helpers()
        leftovers = await HostFileReader(client, clock=lambda: OLD).remove_leftovers()
        still_there = list(daemon.helpers())

    assert [(item.container_id, item.removed) for item in leftovers] == [(running.id, False)]
    assert still_there == [running] and daemon.helpers() == []


async def test_the_image_choice_prefers_a_small_image_that_declares_no_volume(
    daemon: VolumeEngine, client: EngineClient
) -> None:
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED, size=10)

    assert await helper_image(client) == PLAIN_IMAGE_ID


async def test_with_every_image_declaring_a_volume_the_smallest_is_used(
    daemon: VolumeEngine, client: EngineClient
) -> None:
    daemon.add_image(PLAIN_IMAGE, PLAIN_IMAGE_ID, DECLARED)
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, DECLARED, size=5000)

    assert await helper_image(client) == PLAIN_IMAGE_ID


async def test_no_image_on_the_host_is_helper_image_missing() -> None:
    empty = VolumeEngine()
    async with EngineClient(ENV_ID, empty.bundle()) as bare:
        with pytest.raises(HelperImageMissing):
            await helper_image(bare)


async def test_only_a_bounded_number_of_images_is_inspected(
    daemon: VolumeEngine, client: EngineClient
) -> None:
    for number in range(MAX_IMAGE_INSPECTS + 5):
        daemon.add_image(
            f"declared-{number}:1", f"sha256:{number + 1:064x}", DECLARED, size=1 + number
        )
    daemon.add_image(PLAIN_IMAGE, IMAGE_ID, DECLARED, size=10_000)

    await helper_image(client)

    inspected = [
        c for c in daemon.calls("GET") if c.path.startswith("/images/") and c.path != "/images/json"
    ]
    assert len(inspected) == MAX_IMAGE_INSPECTS


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param(MOUNT_POINT, id="the-mount-point"),
        pytest.param(f"{MOUNT_POINT}/sub", id="below-it"),
        pytest.param(f"/{MOUNT_POINT.lstrip('/')}//sub/./deeper", id="unclean-spelling"),
        pytest.param("/declared/../dockmin-read", id="parent-component"),
    ],
)
async def test_an_image_declaring_a_volume_at_or_below_the_mount_point_is_never_the_helper_image(
    daemon: VolumeEngine, client: EngineClient, declared: str
) -> None:
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, {"Volumes": {declared: {}}}, size=1)

    assert await helper_image(client) == PLAIN_IMAGE_ID
    with pytest.raises(HelperImageUnusable):
        await check_helper_image(client, VOLUME_IMAGE_ID)


async def test_a_volume_beside_the_mount_point_does_not_disqualify_an_image(
    daemon: VolumeEngine, client: EngineClient
) -> None:
    sibling: dict[str, Any] = {"Volumes": {f"{MOUNT_POINT}-other": {}}}
    daemon.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, sibling, size=1)

    await check_helper_image(client, VOLUME_IMAGE_ID)
    assert await helper_image(client) == PLAIN_IMAGE_ID


async def test_with_only_unusable_images_on_the_host_there_is_no_helper_image() -> None:
    engine = VolumeEngine()
    engine.add_image(VOLUME_IMAGE, VOLUME_IMAGE_ID, {"Volumes": {MOUNT_POINT: {}}})
    async with EngineClient(ENV_ID, engine.bundle()) as only:
        with pytest.raises(HelperImageMissing):
            await helper_image(only)


async def test_checking_an_image_the_host_no_longer_holds_is_helper_image_missing(
    client: EngineClient,
) -> None:
    with pytest.raises(HelperImageMissing):
        await check_helper_image(client, VOLUME_IMAGE_ID)
