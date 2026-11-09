"""VolumeImportService over a fake daemon and the seeded database."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, SelfProtected, ValidationFailed
from app.self_identity import SelfIdentity
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.volume_import import (
    EMPTY_READ_BYTES,
    IMPORT_LABEL,
    ImportChangedSinceReview,
    ImportReview,
    ImportUploadGone,
    VolumeImported,
    VolumeImportFailed,
    VolumeImportRefused,
    VolumeImportService,
    mount_confirmation,
    option_rows,
    options_of_rows,
)
from app.services.volume_import_archive import inspect_volume_archive
from app.services.volume_import_store import (
    CAPACITY,
    LIFETIME_SECONDS,
    SPOOL_DIRECTORY,
    VolumeImportStore,
    spool_upload,
)
from app.services.volume_views import VolumeNameInUse
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, control, volume_key
from tests.docker_engine.container_engine import ENGINE_TEXT, VOLUME_CREATED
from tests.docker_engine.fake_engine import json_reply
from tests.object_support import rows_of
from tests.self_support import Held, home_identity
from tests.services.conftest import ContainerRig
from tests.volume_export_support import MOUNT_POINT, volume_tar
from tests.volume_import_support import (
    FIXTURE,
    Entry,
    ImportEngine,
    export_archive,
    file,
    manifest_document,
    plain_tar,
)

IMAGE = "busybox:1"
SESSION = 77
OTHER_SESSION = 78
TARGET = "restored"
SENTINEL = "option-value-sentinel-4417"
FIXTURE_FILES = {"a.txt", "secret.txt", "dir", "dir/sub/deep/n.txt", "rel", "abs", "hard"}
ADMIN_SESSION = dataclasses.replace(ADMIN, session_id=SESSION)
OTHER_ADMIN_SESSION = dataclasses.replace(ADMIN, session_id=OTHER_SESSION)
STANDARD_SESSION = dataclasses.replace(STANDARD, session_id=SESSION)
STACK_LABELS = {
    "com.docker.compose.project": "shop",
    "com.docker.stack.namespace": "shop",
    "dockmin.owner": "someone",
}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[ImportEngine]:
    daemon = ImportEngine()
    daemon.add_image(IMAGE)
    yield daemon
    daemon.release_gates()


@pytest.fixture
def store_clock() -> Clock:
    return Clock()


@pytest.fixture
async def store(store_clock: Clock) -> AsyncIterator[VolumeImportStore]:
    held = VolumeImportStore(store_clock)
    yield held
    await held.aclose()


def importer(
    rig: ContainerRig,
    store: VolumeImportStore,
    tmp_path: Path,
    identity: SelfIdentity | None = None,
) -> VolumeImportService:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return VolumeImportService(
        db=rig.db,
        database=rig.database,
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.locks,
        controls=rig.access.controls,
        activity=rig.recorder,
        clock=rig.runtime.clock,
        store=store,
        data_dir=data_dir,
        self_identity=None if identity is None else Held(identity),
    )


async def parts(content: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(content), 8192):
        yield content[start : start + 8192]


async def upload(service: VolumeImportService, content: bytes | None = None) -> str:
    data = FIXTURE.read_bytes() if content is None else content
    return await service.receive(ADMIN_SESSION, SESSION, ENV_A, parts(data))


def applied_labels(body: Mapping[str, Any]) -> dict[str, str]:
    return {k: v for k, v in body["Labels"].items() if k != IMPORT_LABEL}


def confirmation_of(review: ImportReview, options: Mapping[str, str] | None = None) -> str:
    rows = option_rows(dict(review.options)) if options is None else options
    needed = mount_confirmation(review.digest, options_of_rows(rows))
    return "" if needed is None else needed.token


async def run(
    service: VolumeImportService,
    upload_id: str,
    name: str = TARGET,
    *,
    driver: str | None = None,
    options: Mapping[str, str] | None = None,
    expected_digest: str | None = None,
    confirm_mount: str | None = None,
) -> VolumeImported:
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    return await service.import_volume(
        ADMIN_SESSION,
        ENV_A,
        upload_id,
        name,
        review.digest if expected_digest is None else expected_digest,
        driver=driver,
        options=options,
        confirm_mount=confirmation_of(review, options) if confirm_mount is None else confirm_mount,
        helper_image=review.helper_image,
    )


def spooled(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "data" / SPOOL_DIRECTORY).glob("*"))


async def held(store: VolumeImportStore, upload_id: str) -> object:
    return await store.get(
        upload_id, user_id=ADMIN.user_id, session_id=SESSION, environment_id=ENV_A
    )


async def test_the_fixture_archive_becomes_a_volume_holding_its_files(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    result = await run(service, upload_id)

    files = host.files_of(TARGET)
    assert set(files) == FIXTURE_FILES
    assert files["a.txt"].data == b"alpha\n" and files["dir/sub/deep/n.txt"].data
    assert (files["secret.txt"].mode, files["secret.txt"].uid, files["secret.txt"].gid) == (
        0o600,
        1000,
        1000,
    )
    assert (files["dir"].uid, files["dir"].gid) == (1000, 2000)
    assert files["hard"].linkname == "a.txt" and files["abs"].linkname == "/etc/passwd"
    assert host.put_queries == [{"path": MOUNT_POINT, "noOverwriteDirNonDir": "true"}]
    assert (result.name, result.members, result.bytes, result.control_created) == (
        TARGET,
        7,
        19,
        True,
    )
    owned = await rig.access.controls.get(ADMIN, volume_key(TARGET))
    assert owned is not None and owned.owner_user_id == ADMIN.user_id


async def test_the_volume_is_created_with_the_archives_driver_options_and_labels(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    await run(service, await upload(service))

    (body,) = host.volume_bodies()
    assert body["Name"] == TARGET and body["Driver"] == "local"
    assert body["DriverOpts"] == {
        "device": "//10.0.0.5/share",
        "o": "addr=10.0.0.5,username=svc,vers=3.0",
        "type": "cifs",
    }
    assert applied_labels(body) == {"team": "core"}


async def test_the_helper_is_removed_and_the_upload_is_gone_after_a_successful_import(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    await run(service, upload_id)

    assert host.helpers() == [] and rig.locks.holder_of(ObjectKey(ENV_A, "volume", TARGET)) is None
    assert [c.query for c in host.calls("DELETE") if "/containers/" in c.path] == [
        {"force": "false", "v": "true"}
    ]
    assert await held(store, upload_id) is None and spooled(tmp_path) == []


async def test_the_row_carries_the_import_and_no_option_value(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    archive = export_archive(
        file("a"), manifest=manifest_document(options={"o": "addr=1.2.3.4", "type": "nfs"})
    )
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service, archive)
    edited = option_rows({"o": SENTINEL, "type": "none", "device": "/srv/other"})

    await run(service, upload_id, options=edited)

    (row,) = await rows_of(rig.db, "volume_imported")
    detail = row["detail"]
    assert (row["result"], row["target_type"], row["target_id"], row["environment_id"]) == (
        "success",
        "volume",
        TARGET,
        ENV_A,
    )
    assert {k: detail[k] for k in ("name", "driver", "members", "bytes", "format_version")} == {
        "name": TARGET,
        "driver": "local",
        "members": 1,
        "bytes": 6,
        "format_version": 1,
    }
    assert detail["labels_dropped"] == 0 and detail["option_keys"] == "o,type,device"
    assert SENTINEL not in str(row) and SENTINEL in str(host.volume_bodies())


async def test_stack_and_product_labels_are_never_applied_and_are_listed_as_dropped(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    archive = export_archive(
        file("a"), manifest=manifest_document(labels={"team": "core", **STACK_LABELS})
    )
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service, archive)

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    result = await run(service, upload_id)

    assert review.labels_dropped == tuple(sorted(STACK_LABELS))
    assert result.labels_dropped == tuple(sorted(STACK_LABELS))
    assert applied_labels(host.volume_bodies()[0]) == {"team": "core"}
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["labels_dropped"] == len(STACK_LABELS)


async def test_the_review_names_what_is_created_and_what_is_dropped(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.add_volume("shop-data")
    service = importer(rig, store, tmp_path)

    review = await run_review(service)

    assert (review.name, review.driver, review.name_taken) == ("shop-data", "local", True)
    assert review.removed_option_keys == ("o.password",)
    assert dict(review.options)["type"] == "cifs" and review.labels == (("team", "core"),)
    assert review.summary.members == 7 and review.helper_image is not None


async def run_review(service: VolumeImportService) -> ImportReview:
    return await service.review(ADMIN_SESSION, SESSION, ENV_A, await upload(service))


async def test_an_existing_name_is_refused_without_adopting_it_and_the_upload_is_kept(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.add_volume("taken")
    host.route(
        "POST",
        "/volumes/create",
        json_reply(
            {
                "Name": "taken",
                "Driver": "local",
                "Mountpoint": "/x",
                "CreatedAt": VOLUME_CREATED,
                "Labels": {},
                "Scope": "local",
            },
            201,
        ),
    )
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(VolumeNameInUse):
        await run(service, upload_id, "taken")

    assert host.calls("POST", "/volumes/create") == [] and "create" not in host.verbs()
    assert host.put_requests() == [] and host.written == {}
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "name_in_use")
    assert await held(store, upload_id) is not None


async def test_another_name_on_the_review_after_a_collision_imports_the_same_upload(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.add_volume("taken")
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    with pytest.raises(VolumeNameInUse):
        await run(service, upload_id, "taken")

    result = await run(service, upload_id, "taken-2")

    assert result.name == "taken-2" and "taken" not in host.written
    assert set(host.files_of("taken-2")) == FIXTURE_FILES


async def test_a_control_left_on_the_name_by_an_earlier_volume_is_replaced(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    await control(rig.access, volume_key(TARGET), level="write", owner=PEER.user_id)
    service = importer(rig, store, tmp_path)

    await run(service, await upload(service))

    owned = await rig.access.controls.get(ADMIN, volume_key(TARGET))
    assert owned is not None and owned.owner_user_id == ADMIN.user_id
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["stale_control_replaced"] is True


async def test_an_archive_of_a_plugin_driver_volume_is_refused_at_upload(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    with pytest.raises(ValidationFailed) as refused:
        await upload(service, export_archive(file("a"), manifest=manifest_document(driver="nfs")))

    assert "local driver" in refused.value.fields["archive"] and spooled(tmp_path) == []


async def test_a_plugin_driver_chosen_on_the_review_is_refused_before_anything_is_created(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(ValidationFailed) as refused:
        await run(service, upload_id, driver="vieux/sshfs:latest")

    assert set(refused.value.fields) == {"driver"} and "create" not in host.verbs()
    assert host.calls("POST", "/volumes/create") == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "invalid"


async def test_a_refused_archive_names_the_member_and_leaves_no_file(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    with pytest.raises(ValidationFailed) as refused:
        await upload(service, export_archive(Entry("data/a/../x")))

    assert "data/a/../x" in refused.value.fields["archive"] and spooled(tmp_path) == []
    assert host.requests == [] and await rows_of(rig.db) == []


async def test_a_plain_tar_is_refused_as_not_a_volume_export(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    with pytest.raises(ValidationFailed) as refused:
        await upload(service, plain_tar(file("a")))

    assert "volume export" in refused.value.fields["archive"] and spooled(tmp_path) == []


async def test_a_newer_manifest_version_is_refused_with_the_version_named(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    archive = export_archive(file("a"), manifest=manifest_document(format_version=9))

    with pytest.raises(ValidationFailed) as refused:
        await upload(service, archive)

    assert "version is 9" in refused.value.fields["archive"]


async def test_a_failed_write_removes_the_volume_this_import_created_and_records_it(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.fail("put_archive", 500)
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(VolumeImportFailed) as failed:
        await run(service, upload_id)

    assert failed.value.volume_removed and "was removed" in str(failed.value)
    assert ENGINE_TEXT not in str(failed.value)
    assert TARGET not in host.volumes and host.helpers() == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "engine_error")
    assert (row["detail"]["volume_removed"], row["detail"]["helper_removed"]) == (True, True)
    assert await held(store, upload_id) is None and spooled(tmp_path) == []
    assert await rig.access.controls.get(ADMIN, volume_key(TARGET)) is None


async def test_a_write_that_fails_part_way_leaves_nothing_behind(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.fail_after_members = 2
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeImportFailed):
        await run(service, await upload(service))

    assert len(host.written[TARGET]) == 2 and TARGET not in host.volumes
    assert host.helpers() == []


async def test_a_failed_write_whose_volume_cannot_be_removed_says_so(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.fail("put_archive", 500)
    host.fail("volume_remove", 500)
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeImportFailed) as failed:
        await run(service, await upload(service))

    assert not failed.value.volume_removed and "could not be removed" in str(failed.value)
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["volume_removed"] is False and TARGET in host.volumes


async def test_a_helper_the_engine_refuses_removes_the_created_volume(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.fail("create", 400)
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeImportFailed):
        await run(service, await upload(service))

    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "helper_refused" and TARGET not in host.volumes


async def test_a_request_cancelled_while_the_data_is_written_removes_the_helper_and_the_volume(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    gate = host.hold("put_archive")
    task = asyncio.ensure_future(
        service.import_volume(
            ADMIN_SESSION,
            ENV_A,
            upload_id,
            TARGET,
            review.digest,
            confirm_mount=confirmation_of(review),
        )
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.helpers() == [] and TARGET not in host.volumes
    assert rig.locks.holder_of(ObjectKey(ENV_A, "volume", TARGET)) is None
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "interrupted")
    assert row["detail"]["volume_removed"] is True
    assert await held(store, upload_id) is None and spooled(tmp_path) == []


async def test_a_request_cancelled_while_the_volume_is_created_removes_it(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    gate = host.hold("volume_create")
    task = asyncio.ensure_future(
        service.import_volume(
            ADMIN_SESSION,
            ENV_A,
            upload_id,
            TARGET,
            review.digest,
            confirm_mount=confirmation_of(review),
        )
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert TARGET not in host.volumes and host.put_requests() == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "interrupted")


async def test_a_host_with_no_image_is_refused_before_anything_is_created_and_the_upload_is_kept(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    host.image_store.clear()
    host.images.clear()

    with pytest.raises(VolumeImportRefused):
        await run(service, upload_id)

    assert host.calls("POST", "/volumes/create") == [] and "create" not in host.verbs()
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "helper_image_missing"
    assert await held(store, upload_id) is not None


async def test_an_upload_that_is_not_the_one_reviewed_is_refused(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(ImportChangedSinceReview):
        await run(service, upload_id, expected_digest="0" * 64)

    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "changed_since_review"
    assert host.calls("POST", "/volumes/create") == []


async def test_an_upload_is_usable_only_by_the_session_that_made_it(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(ImportUploadGone):
        await service.review(OTHER_ADMIN_SESSION, OTHER_SESSION, ENV_A, upload_id)
    with pytest.raises(ImportUploadGone):
        await service.import_volume(OTHER_ADMIN_SESSION, ENV_A, upload_id, TARGET, "0" * 64)
    await service.cancel(OTHER_ADMIN_SESSION, OTHER_SESSION, ENV_A, upload_id)

    assert await held(store, upload_id) is not None and host.calls("POST") == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "upload_gone"


async def test_an_expired_upload_is_gone_and_its_file_removed(
    rig: ContainerRig,
    host: ImportEngine,
    store: VolumeImportStore,
    tmp_path: Path,
    store_clock: Clock,
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    store_clock.now = LIFETIME_SECONDS + 1

    with pytest.raises(ImportUploadGone):
        await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    assert spooled(tmp_path) == []


async def test_a_cancelled_upload_is_discarded_and_nothing_is_recorded(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    await service.cancel(ADMIN_SESSION, SESSION, ENV_A, upload_id)

    assert await held(store, upload_id) is None and spooled(tmp_path) == []
    assert await rows_of(rig.db) == [] and host.requests == []


async def test_a_standard_user_is_refused_everywhere_without_a_row_or_a_file(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(PermissionDenied):
        await service.receive(STANDARD_SESSION, SESSION, ENV_A, parts(FIXTURE.read_bytes()))
    with pytest.raises(PermissionDenied):
        await service.review(STANDARD_SESSION, SESSION, ENV_A, upload_id)
    with pytest.raises(PermissionDenied):
        await service.import_volume(STANDARD_SESSION, ENV_A, upload_id, TARGET, "0" * 64)
    with pytest.raises(PermissionDenied):
        await service.cancel(STANDARD_SESSION, SESSION, ENV_A, upload_id)

    assert await rows_of(rig.db) == [] and host.calls("POST") == []
    assert len(spooled(tmp_path)) == 1


async def test_a_name_another_request_holds_is_busy_and_nothing_is_created(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    holder = LockHolder(UserActor(PEER.user_id), "peer", "create")

    async with rig.locks.hold(ObjectKey(ENV_A, "volume", TARGET), holder):
        with pytest.raises(ObjectBusy):
            await run(service, upload_id)

    assert host.calls("POST", "/volumes/create") == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "object_busy"


async def test_a_volume_bound_to_a_path_dockmin_mounts_is_refused_before_anything_is_created(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    identity = dataclasses.replace(
        home_identity(), mount_sources={ENV_A: frozenset({"/srv/dockmin"})}
    )
    service = importer(rig, store, tmp_path, identity)
    upload_id = await upload(service)
    bound = option_rows({"type": "none", "o": "bind", "device": "/srv/dockmin/data"})

    with pytest.raises(SelfProtected):
        await run(service, upload_id, options=bound)

    assert host.calls("POST", "/volumes/create") == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["reason"] == "self_protected"


async def test_a_digest_that_is_not_ascii_is_a_changed_upload_and_not_an_error(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    with pytest.raises(ImportChangedSinceReview):
        await run(service, await upload(service), expected_digest="é" * 64)

    assert host.calls("POST", "/volumes/create") == []


async def test_label_rows_posted_with_the_review_are_ignored(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    posted = {
        **option_rows({"type": "none"}),
        "label-0-key": "com.docker.compose.project",
        "label-0-value": "shop",
    }

    await run(service, await upload(service), options=posted)

    assert applied_labels(host.volume_bodies()[0]) == {"team": "core"}
    assert host.volume_bodies()[0]["DriverOpts"] == {"type": "none"}


OTHER_IMAGE_ID = "sha256:" + "b" * 64
UNUSABLE_IMAGE_ID = "sha256:" + "c" * 64
ABSENT_IMAGE_ID = "sha256:" + "d" * 64
RACED_LABELS = {"owner": "someone-else"}
CIFS_DEVICE = "//10.0.0.5/share"


def _host_options(device: str, kind: str = "none") -> dict[str, str]:
    return option_rows({"type": kind, "o": "bind", "device": device})


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        pytest.param({}, False, id="no-options"),
        pytest.param({"type": "tmpfs", "o": "size=1m"}, False, id="memory-only"),
        pytest.param({"o": "bind"}, False, id="mount-flags-alone"),
        pytest.param({"type": "none", "o": "bind", "device": "/srv/x"}, True, id="bind"),
        pytest.param({"type": "nfs", "device": ":/export"}, True, id="nfs"),
        pytest.param({"type": "cifs"}, True, id="type-without-device"),
        pytest.param({"device": "/srv/x"}, True, id="device-alone"),
    ],
)
def test_only_options_that_bind_a_host_path_or_mount_a_share_need_a_confirmation(
    options: dict[str, str], expected: bool
) -> None:
    assert (mount_confirmation("digest", options) is not None) is expected


def test_a_confirmation_does_not_change_with_the_mount_flags() -> None:
    base = mount_confirmation("digest", {"type": "none", "device": "/srv/a"})

    assert base is not None and (base.device, base.type) == ("/srv/a", "none")
    assert mount_confirmation("digest", {"type": "none", "device": "/srv/a", "o": "x"}) == base


@pytest.mark.parametrize(
    ("digest", "options"),
    [
        pytest.param("other", {"type": "none", "device": "/srv/a"}, id="another-archive"),
        pytest.param("digest", {"type": "none", "device": "/srv/b"}, id="another-device"),
        pytest.param("digest", {"type": "nfs", "device": "/srv/a"}, id="another-type"),
    ],
)
def test_a_confirmation_is_bound_to_the_archive_the_type_and_the_device(
    digest: str, options: dict[str, str]
) -> None:
    base = mount_confirmation("digest", {"type": "none", "device": "/srv/a"})
    other = mount_confirmation(digest, options)

    assert base is not None and other is not None and other.token != base.token


async def test_options_that_bind_a_host_path_are_refused_without_the_confirmation_of_that_path(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    edited = _host_options("/srv/elsewhere")
    stale = confirmation_of(review)

    with pytest.raises(ValidationFailed) as unconfirmed:
        await run(service, upload_id, options=edited, confirm_mount="")
    with pytest.raises(ValidationFailed) as other_path:
        await run(service, upload_id, options=edited, confirm_mount=stale)

    assert set(unconfirmed.value.fields) == set(other_path.value.fields) == {"confirm_mount"}
    assert host.calls("POST", "/volumes/create") == [] and "create" not in host.verbs()
    rows = await rows_of(rig.db, "volume_imported")
    assert [r["detail"]["reason"] for r in rows] == ["mount_not_confirmed"] * 2
    assert await held(store, upload_id) is not None


async def test_the_confirmation_of_the_edited_path_lets_the_import_run(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    edited = _host_options("/srv/elsewhere")

    await run(service, upload_id, options=edited)

    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["host_mount"] is True
    assert host.volume_bodies()[0]["DriverOpts"]["device"] == "/srv/elsewhere"


async def test_a_volume_with_no_options_needs_no_confirmation(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    await run(service, await upload(service), options={})

    (row,) = await rows_of(rig.db, "volume_imported")
    assert "host_mount" not in row["detail"] and TARGET in host.volumes


@pytest.mark.parametrize("options", [{}, _host_options("/srv/data")])
async def test_a_new_volume_that_already_holds_files_is_refused_removed_and_nothing_is_written(
    rig: ContainerRig,
    host: ImportEngine,
    store: VolumeImportStore,
    tmp_path: Path,
    options: dict[str, str],
) -> None:
    host.serve_volume(TARGET, volume_tar())
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(VolumeImportRefused) as refused:
        await run(service, upload_id, options=options)

    assert refused.value.reason == "volume_not_empty" and "were not touched" in str(refused.value)
    assert host.put_requests() == [] and host.written == {}
    assert TARGET not in host.volumes and host.helpers() == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "volume_not_empty")
    assert row["detail"]["volume_removed"] is True
    assert await held(store, upload_id) is not None


async def test_a_volume_that_holds_more_than_the_read_cap_is_refused_without_reading_it_all(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.serve_volume(TARGET, volume_tar({"big": b"x" * (4 * EMPTY_READ_BYTES)}))
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeImportRefused):
        await run(service, await upload(service))

    assert host.put_requests() == []


async def test_a_failed_write_to_a_host_path_says_its_files_stay_there(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.fail("put_archive", 500)
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeImportFailed) as host_backed:
        await run(service, await upload(service))
    with pytest.raises(VolumeImportFailed) as plain:
        await run(service, await upload(service), "plain", options={})

    assert "host path or share are not removed" in str(host_backed.value)
    assert "host path" not in str(plain.value) and "was removed" in str(plain.value)


async def test_a_volume_that_appears_between_the_check_and_the_create_is_not_adopted(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.on("volume_create", lambda: host.add_volume(TARGET, labels=RACED_LABELS))
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)

    with pytest.raises(VolumeNameInUse):
        await run(service, upload_id)

    assert host.put_requests() == [] and host.written == {} and host.helpers() == []
    assert TARGET in host.volumes and host.calls("DELETE", f"/volumes/{TARGET}") == []
    (row,) = await rows_of(rig.db, "volume_imported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "name_in_use")
    assert await rig.access.controls.get(ADMIN, volume_key(TARGET)) is None
    assert await held(store, upload_id) is not None


async def test_a_volume_created_by_another_import_of_the_same_name_is_not_adopted_either(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.on(
        "volume_create",
        lambda: host.add_volume(
            TARGET,
            labels={IMPORT_LABEL: "another-imports-token"},
            options={
                "type": "cifs",
                "device": CIFS_DEVICE,
                "o": "addr=10.0.0.5,username=svc,vers=3.0",
            },
        ),
    )
    service = importer(rig, store, tmp_path)

    with pytest.raises(VolumeNameInUse):
        await run(service, await upload(service))

    assert host.put_requests() == [] and host.calls("DELETE", f"/volumes/{TARGET}") == []


async def test_a_second_import_of_an_upload_that_is_being_imported_is_refused(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    gate = host.hold("put_archive")
    first = asyncio.ensure_future(run(service, upload_id))
    await gate.entered.wait()

    with pytest.raises(VolumeImportRefused) as second:
        await service.import_volume(
            ADMIN_SESSION,
            ENV_A,
            upload_id,
            "second-copy",
            review.digest,
            confirm_mount=confirmation_of(review),
            helper_image=review.helper_image,
        )
    running_after_refusal = (await held(store, upload_id)).importing  # type: ignore[attr-defined]
    gate.release.set()
    result = await first

    assert second.value.reason == "import_running" and running_after_refusal is True
    assert result.name == TARGET and "second-copy" not in host.volumes
    rows = await rows_of(rig.db, "volume_imported")
    assert [(r["result"], r["detail"].get("reason")) for r in rows] == [
        ("failure", "import_running"),
        ("success", None),
    ]


async def test_a_discard_while_the_upload_is_being_imported_leaves_it_and_says_so(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    gate = host.hold("put_archive")
    first = asyncio.ensure_future(run(service, upload_id))
    await gate.entered.wait()

    discarded = await service.cancel(ADMIN_SESSION, SESSION, ENV_A, upload_id)
    kept = await held(store, upload_id) is not None
    gate.release.set()
    await first

    assert discarded is False and kept
    assert await service.cancel(ADMIN_SESSION, SESSION, ENV_A, upload_id) is True


async def test_the_helper_is_created_from_the_image_the_review_named(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    host.add_image("other:1", OTHER_IMAGE_ID, size=9000)
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)

    await service.import_volume(
        ADMIN_SESSION,
        ENV_A,
        upload_id,
        TARGET,
        review.digest,
        confirm_mount=confirmation_of(review),
        helper_image=OTHER_IMAGE_ID,
    )

    assert review.helper_image != OTHER_IMAGE_ID
    assert [body["Image"] for body in host.bodies()] == [OTHER_IMAGE_ID]


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        pytest.param(UNUSABLE_IMAGE_ID, "helper_image_unusable", id="declares-a-volume-under-it"),
        pytest.param(ABSENT_IMAGE_ID, "helper_image_missing", id="gone-from-the-host"),
        pytest.param("", "helper_image_missing", id="none-named"),
    ],
)
async def test_an_image_the_review_did_not_name_validly_is_refused_before_anything_is_created(
    rig: ContainerRig,
    host: ImportEngine,
    store: VolumeImportStore,
    tmp_path: Path,
    image: str,
    reason: str,
) -> None:
    host.add_image("bad:1", UNUSABLE_IMAGE_ID, {"Volumes": {f"{MOUNT_POINT}/data": {}}}, size=1)
    service = importer(rig, store, tmp_path)
    upload_id = await upload(service)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, upload_id)

    with pytest.raises(VolumeImportRefused) as refused:
        await service.import_volume(
            ADMIN_SESSION,
            ENV_A,
            upload_id,
            TARGET,
            review.digest,
            confirm_mount=confirmation_of(review),
            helper_image=image,
        )

    assert review.helper_image != UNUSABLE_IMAGE_ID and refused.value.reason == reason
    assert host.calls("POST", "/volumes/create") == [] and "create" not in host.verbs()
    assert await held(store, upload_id) is not None


async def test_a_success_row_always_says_whether_a_stale_control_was_replaced(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)

    await run(service, await upload(service))

    (row,) = await rows_of(rig.db, "volume_imported")
    assert row["detail"]["stale_control_replaced"] is False


async def test_an_upload_is_refused_while_other_users_uploads_fill_the_store_and_leaves_no_file(
    rig: ContainerRig, host: ImportEngine, store: VolumeImportStore, tmp_path: Path
) -> None:
    service = importer(rig, store, tmp_path)
    fixture = inspect_volume_archive(FIXTURE)
    for number in range(CAPACITY):
        stored_upload = await spool_upload(parts(b"x"), tmp_path / "data", f"{number:032x}")
        await store.hold(
            f"{number:032x}",
            user_id=ADMIN.user_id + 100 + number,
            session_id=number,
            environment_id=ENV_A,
            upload=stored_upload,
            summary=fixture,
        )
    before = len(spooled(tmp_path))

    with pytest.raises(ValidationFailed) as refused:
        await upload(service)

    assert "waiting for review" in refused.value.fields["archive"]
    assert len(spooled(tmp_path)) == before
