"""VolumeExportService over a fake daemon and the seeded database."""

import asyncio
import dataclasses
import json
import random
import tarfile
from collections.abc import Iterator, MutableMapping
from contextlib import AsyncExitStack
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected
from app.self_identity import SelfIdentity
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.volume_export import (
    ExportSlots,
    VolumeExportBusy,
    VolumeExportRefused,
    VolumeExportService,
    VolumeExportStream,
)
from app.services.volume_export_writer import StreamMalformed
from app.services.volume_views import VolumeChangedSinceReview
from app.web.limits import SendIdleTimeoutMiddleware
from app.web.volume_export_routes import _ExportResponse
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.archive_support import Member
from tests.docker_engine.container_engine import VOLUME_CREATED
from tests.docker_engine.fake_engine import error_reply
from tests.object_support import add_container, rows_of, volume_mount
from tests.self_support import SELF_VOLUME, Held, home_identity
from tests.services.conftest import ContainerRig
from tests.volume_export_support import (
    MOUNT_POINT,
    NOTE,
    NOTE_FILE,
    VolumeEngine,
    unpack,
    unterminated,
    volume_tar,
)

IMAGE = "busybox:1"
VOLUME = "data"
SENTINEL = "cifs-password-sentinel-91"
OTHER_CREATED_AT = "2024-06-01T00:00:00Z"
RUNNING_NAME = "web"
STOPPED_NAME = "old"
KEY = ObjectKey(ENV_A, "volume", VOLUME)
OTHER_IMAGE_ID = "sha256:" + "b" * 64
UNUSABLE_IMAGE_ID = "sha256:" + "c" * 64
STALL_SECONDS = 0.1


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[VolumeEngine]:
    daemon = VolumeEngine()
    daemon.add_image(IMAGE)
    daemon.add_volume(VOLUME)
    daemon.serve_volume(VOLUME, volume_tar())
    yield daemon
    daemon.release_gates()


def exporter(
    rig: ContainerRig, identity: SelfIdentity | None = None, slots: ExportSlots | None = None
) -> VolumeExportService:
    return VolumeExportService(
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.locks,
        activity=rig.recorder,
        clock=rig.runtime.clock,
        self_identity=None if identity is None else Held(identity),
        slots=ExportSlots() if slots is None else slots,
    )


async def begin(
    service: VolumeExportService,
    name: str = VOLUME,
    environment_id: int = ENV_A,
    created: str = VOLUME_CREATED,
) -> VolumeExportStream:
    return await service.start(ADMIN, environment_id, name, expected_created_at=created)


async def drain(stream: VolumeExportStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


def known_mounts(identity: SelfIdentity) -> SelfIdentity:
    return dataclasses.replace(identity, mount_sources={ENV_A: frozenset({"/srv/dockmin"})})


async def test_an_export_streams_the_volume_with_the_manifest_first(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.volume_labels[VOLUME] = {"team": "core", "com.docker.compose.project": "shop"}
    stream = await begin(exporter(rig))

    members = unpack(await drain(stream))

    manifest = json.loads(members["manifest.json"][1])
    assert list(members)[:2] == ["manifest.json", "data"]
    assert members[f"data/{NOTE_FILE}"][1] == NOTE
    assert (manifest["name"], manifest["driver"], manifest["labels"]) == (
        VOLUME,
        "local",
        {"team": "core"},
    )


async def test_the_started_row_is_written_before_a_byte_is_read_and_the_finished_row_after(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({NOTE_FILE: NOTE}, Member("pipe", type=tarfile.FIFOTYPE)))
    stream = await begin(exporter(rig))
    helper_id = stream.helper.container_id

    before = [(r["action"], r["result"]) for r in await rows_of(rig.db)]
    archive = await drain(stream)
    after = await rows_of(rig.db)

    assert before == [("volume_export_started", "success")]
    started, finished = after
    assert (started["target_type"], started["target_id"], started["environment_id"]) == (
        "volume",
        VOLUME,
        ENV_A,
    )
    assert started["detail"]["helper_container"] == helper_id
    assert (finished["action"], finished["result"]) == ("volume_exported", "success")
    assert finished["detail"] | {"bytes": 0} == {
        "name": VOLUME,
        "driver": "local",
        "format_version": 1,
        "bytes": 0,
        "source_bytes": len(volume_tar({NOTE_FILE: NOTE}, Member("pipe", type=tarfile.FIFOTYPE))),
        "skipped": 1,
        "completed": True,
        "helper_removed": True,
    }
    assert finished["detail"]["bytes"] == len(archive)


async def test_the_helper_and_the_slot_and_the_lock_are_released_when_the_export_completes(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    during = (host.helpers() != [], rig.locks.holder_of(KEY) is not None)

    await drain(stream)

    assert during == (True, True)
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None
    assert [c.query for c in host.calls("DELETE") if "/containers/" in c.path] == [
        {"force": "false", "v": "true"}
    ]


async def test_a_client_that_leaves_part_way_ends_the_export_and_records_it(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({"big": b"x" * (1 << 20)}))
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    await stream.__anext__()

    await stream.aclose()
    await stream.aclose()

    (finished,) = await rows_of(rig.db, "volume_exported")
    assert (finished["result"], finished["detail"]["reason"]) == ("failure", "interrupted")
    assert finished["detail"]["completed"] is False and finished["detail"]["bytes"] > 0
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None


async def test_a_stream_that_is_not_a_whole_tar_fails_the_export_and_removes_the_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, unterminated(volume_tar()))
    stream = await begin(exporter(rig))

    with pytest.raises(StreamMalformed):
        await drain(stream)

    (finished,) = await rows_of(rig.db, "volume_exported")
    assert (finished["result"], finished["detail"]["reason"]) == ("failure", "malformed_stream")
    assert host.helpers() == []


async def test_a_request_cancelled_while_the_helper_is_created_leaves_nothing_behind(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    slots = ExportSlots()
    gate = host.hold("create")
    task = asyncio.ensure_future(begin(exporter(rig, slots=slots)))
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None


async def test_a_standard_user_is_refused_without_a_row_or_a_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)

    with pytest.raises(PermissionDenied):
        await service.start(STANDARD, ENV_A, VOLUME, expected_created_at=VOLUME_CREATED)
    with pytest.raises(PermissionDenied):
        await service.confirmation(STANDARD, ENV_A, VOLUME)

    assert await rows_of(rig.db) == [] and "create" not in host.verbs()


async def test_a_volume_that_is_not_there_or_not_named_validly_is_not_found(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)

    with pytest.raises(ResourceNotFound):
        await begin(service, "missing")
    with pytest.raises(ResourceNotFound):
        await begin(service, "../escape")

    assert "create" not in host.verbs()


async def test_a_volume_that_changed_since_the_review_is_refused_before_a_helper_exists(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    with pytest.raises(VolumeChangedSinceReview):
        await begin(exporter(rig), created=OTHER_CREATED_AT)

    (row,) = await rows_of(rig.db, "volume_export_started")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "changed_since_review")
    assert "create" not in host.verbs() and rig.locks.holder_of(KEY) is None


async def test_dockmins_own_volume_is_refused_before_a_helper_exists(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume(SELF_VOLUME)
    service = exporter(rig, known_mounts(home_identity(volumes=(SELF_VOLUME,))))

    with pytest.raises(SelfProtected):
        await begin(service, SELF_VOLUME)
    with pytest.raises(SelfProtected):
        await service.confirmation(ADMIN, ENV_A, SELF_VOLUME)

    (row,) = await rows_of(rig.db, "volume_export_started")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_every_volume_is_refused_while_dockmins_own_mounts_are_unknown(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig, home_identity())

    with pytest.raises(SelfProtected):
        await begin(service)

    (row,) = await rows_of(rig.db, "volume_export_started")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_a_volume_bound_to_a_path_dockmin_mounts_is_refused(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("bound", options={"type": "none", "o": "bind", "device": "/srv/dockmin/data"})
    service = exporter(rig, known_mounts(home_identity()))

    with pytest.raises(SelfProtected):
        await begin(service, "bound")

    assert "create" not in host.verbs()


async def test_a_plugin_driver_volume_is_refused_in_the_confirmation_and_the_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("plugged", driver="nfs-plugin")
    service = exporter(rig)

    with pytest.raises(VolumeExportRefused) as refused:
        await service.confirmation(ADMIN, ENV_A, "plugged")
    with pytest.raises(VolumeExportRefused):
        await begin(service, "plugged")

    (row,) = await rows_of(rig.db, "volume_export_started")
    assert refused.value.reason == "plugin_driver" and row["detail"]["reason"] == "plugin_driver"
    assert "create" not in host.verbs()


async def test_a_host_with_no_image_is_refused_before_a_helper_is_created(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.image_store.clear()
    host.images.clear()
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, VOLUME)
    with pytest.raises(VolumeExportRefused):
        await begin(service)

    (row,) = await rows_of(rig.db, "volume_export_started")
    assert confirmation.helper_image is None and not confirmation.can_export
    assert row["detail"]["reason"] == "helper_image_missing" and host.verbs() == []


async def test_running_containers_are_named_on_the_confirmation_and_do_not_stop_the_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    add_container(host, 1, RUNNING_NAME, state="running", mounts=[volume_mount(VOLUME)])
    add_container(host, 2, STOPPED_NAME, state="exited", mounts=[volume_mount(VOLUME)])
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, VOLUME)
    await drain(await begin(service))

    (started,) = await rows_of(rig.db, "volume_export_started")
    assert [user.name for user in confirmation.running] == [RUNNING_NAME]
    assert (confirmation.created_at_text, confirmation.driver) == (VOLUME_CREATED, "local")
    assert started["detail"]["running_containers"] == 1 and started["detail"]["containers"] == 2
    assert host.verbs().count("start") == 0


async def test_a_local_volume_with_a_cifs_password_exports_without_the_password_anywhere(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume(
        "share",
        options={"type": "cifs", "device": "//10.0.0.5/s", "o": f"username=u,password={SENTINEL}"},
    )
    host.serve_volume("share", volume_tar())

    archive = await drain(await begin(exporter(rig), "share"))

    rows = json.dumps(await rows_of(rig.db))
    manifest = json.loads(unpack(archive)["manifest.json"][1])
    assert SENTINEL not in rows and SENTINEL not in json.dumps(host.bodies())
    assert manifest["removed_option_keys"] == ["o"]
    assert "o" not in manifest["options"] and manifest["options"]["type"] == "cifs"
    assert SENTINEL not in json.dumps(manifest)


async def test_a_second_export_on_the_environment_is_refused_until_the_first_ends(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("other")
    host.serve_volume("other", volume_tar())
    service = exporter(rig)
    first = await begin(service)

    with pytest.raises(VolumeExportBusy):
        await begin(service, "other")
    await drain(first)
    await drain(await begin(service, "other"))

    (refusal,) = [
        r for r in await rows_of(rig.db, "volume_export_started") if r["result"] != "success"
    ]
    assert refusal["detail"]["reason"] == "export_busy" and host.helpers() == []


def test_the_instance_cap_refuses_a_third_export_with_a_retry_message() -> None:
    slots = ExportSlots(per_environment=1, per_instance=2)
    first, second = slots.acquire(1), slots.acquire(2)

    with pytest.raises(VolumeExportBusy, match="Try again shortly"):
        slots.acquire(3)
    first.release()
    first.release()
    third = slots.acquire(3)

    assert third is not None and second is not None
    with pytest.raises(VolumeExportBusy):
        slots.acquire(3)


async def test_an_export_of_a_volume_another_action_holds_is_refused_busy_and_frees_its_slot(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    slots = ExportSlots()
    holder = LockHolder(UserActor(ADMIN.user_id), "admin", "remove")

    async with rig.locks.hold(KEY, holder):
        with pytest.raises(ObjectBusy):
            await begin(exporter(rig, slots=slots))

    assert slots.acquire(ENV_A) is not None and "create" not in host.verbs()


async def test_a_volume_in_another_environment_is_exported_independently(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)
    first = await begin(service)
    second = await begin(service, environment_id=ENV_B)

    await drain(first)
    await drain(second)

    assert len(await rows_of(rig.db, "volume_exported")) == 2


async def test_a_response_whose_client_goes_away_closes_the_export_and_the_session(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({"big": b"x" * (1 << 20)}))
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    stack = AsyncExitStack()
    closed: list[bool] = []
    stack.callback(closed.append, True)
    response = _ExportResponse(stream, stack, "volume.tar.gz")
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            raise OSError("client went away")

    async def receive() -> MutableMapping[str, Any]:
        await asyncio.Event().wait()
        return {}

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST", "headers": []}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    headers = dict(sent[0]["headers"])
    assert headers[b"content-disposition"] == b'attachment; filename="volume.tar.gz"'
    assert (
        headers[b"cache-control"] == b"no-store"
        and headers[b"x-content-type-options"] == b"nosniff"
    )
    assert closed == [True] and host.helpers() == [] and slots.acquire(ENV_A) is not None
    (finished,) = await rows_of(rig.db, "volume_exported")
    assert finished["detail"]["completed"] is False


async def test_a_response_that_never_started_still_releases_the_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    response = _ExportResponse(stream, AsyncExitStack(), "volume.tar.gz")

    async def send(message: MutableMapping[str, Any]) -> None:
        raise OSError("gone before the first byte")

    async def receive() -> MutableMapping[str, Any]:
        return {}

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST", "headers": []}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert host.helpers() == [] and slots.acquire(ENV_A) is not None


async def test_a_status_from_docker_when_the_helper_is_created_is_a_refusal_not_an_unreachable_host(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.fail("create", 500)

    with pytest.raises(VolumeExportRefused) as refused:
        await begin(exporter(rig))

    assert refused.value.reason == "helper_refused"
    assert str(refused.value) == "Docker could not mount or read the volume."
    (row,) = await rows_of(rig.db, "volume_export_started")
    assert row["detail"]["reason"] == "helper_refused" and host.helpers() == []
    assert rig.locks.holder_of(KEY) is None


async def test_a_status_from_docker_when_the_volume_is_read_is_a_refusal_and_removes_the_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, error_reply(500))

    with pytest.raises(VolumeExportRefused) as refused:
        await begin(exporter(rig))

    assert refused.value.reason == "unreadable"
    assert str(refused.value) == "Docker could not mount or read the volume."
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None


async def test_the_helper_is_created_from_the_image_the_confirmation_named(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_image("other:1", OTHER_IMAGE_ID, size=9000)

    stream = await exporter(rig).start(
        ADMIN, ENV_A, VOLUME, expected_created_at=VOLUME_CREATED, helper_image=OTHER_IMAGE_ID
    )
    await drain(stream)

    assert [body["Image"] for body in host.bodies()] == [OTHER_IMAGE_ID]


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        pytest.param(UNUSABLE_IMAGE_ID, "helper_image_unusable", id="declares-a-volume-under-it"),
        pytest.param(OTHER_IMAGE_ID, "helper_image_missing", id="gone-from-the-host"),
        pytest.param("", "helper_image_missing", id="none-named"),
    ],
)
async def test_an_image_the_confirmation_did_not_name_validly_is_refused_before_a_helper_exists(
    rig: ContainerRig, host: VolumeEngine, image: str, reason: str
) -> None:
    host.add_image("bad:1", UNUSABLE_IMAGE_ID, {"Volumes": {f"{MOUNT_POINT}/data": {}}}, size=1)

    with pytest.raises(VolumeExportRefused) as refused:
        await exporter(rig).start(
            ADMIN, ENV_A, VOLUME, expected_created_at=VOLUME_CREATED, helper_image=image
        )

    assert refused.value.reason == reason and "create" not in host.verbs()
    assert rig.locks.holder_of(KEY) is None


async def test_a_response_whose_client_stops_reading_frees_the_slot_and_removes_the_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({"big": random.Random(7).randbytes(512 * 1024)}))
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    sent: list[MutableMapping[str, Any]] = []

    async def stalling_send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and any(
            earlier["type"] == "http.response.body" for earlier in sent
        ):
            await asyncio.Event().wait()
        sent.append(message)

    async def receive() -> MutableMapping[str, Any]:
        await asyncio.Event().wait()
        return {}

    response = _ExportResponse(stream, AsyncExitStack(), "volume.tar.gz")
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST", "headers": []}
    await asyncio.wait_for(
        SendIdleTimeoutMiddleware(response, seconds=STALL_SECONDS)(scope, receive, stalling_send),
        timeout=10,
    )

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert len(bodies) == 1 and bodies[0]["more_body"] is True
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None
    (finished,) = await rows_of(rig.db, "volume_exported")
    assert finished["detail"]["completed"] is False and finished["detail"]["helper_removed"] is True
