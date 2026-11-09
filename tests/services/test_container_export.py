"""ContainerExportService over a fake daemon and the seeded database."""

import asyncio
import dataclasses
import json
from collections.abc import Iterator

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected, ValidationFailed
from app.self_identity import SelfIdentity
from app.services.container_export import (
    ContainerChangedSinceReview,
    ContainerExportBusy,
    ContainerExportRefused,
    ContainerExportService,
    ContainerExportStream,
)
from app.services.export_sealing import open_sealed
from app.services.volume_export import ExportSlots
from app.services.volume_export_writer import StreamMalformed
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.container_export_support import (
    BIND_SOURCE,
    IMAGE,
    IMAGE_DIGEST,
    NETWORK,
    NETWORK_PASSWORD,
    PASSPHRASE,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_NAME,
    SECRET_VALUE,
    SOURCE,
    SOURCE_NAME,
    VOLUME,
    archive_text,
    manifest_of,
    stock_source,
)
from tests.object_support import rows_of
from tests.self_support import SELF_ID, Held, home_identity
from tests.services.conftest import ContainerRig
from tests.stack_support import logged_text
from tests.volume_export_support import (
    NOTE,
    NOTE_FILE,
    VolumeEngine,
    unpack,
    unterminated,
    volume_tar,
)

WRONG_LENGTH = "short"
STALE_DIGEST = "0" * 64


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[VolumeEngine]:
    daemon = VolumeEngine()
    stock_source(daemon)
    yield daemon
    daemon.release_gates()


def exporter(
    rig: ContainerRig, identity: SelfIdentity | None = None, slots: ExportSlots | None = None
) -> ContainerExportService:
    return ContainerExportService(
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        creation=rig.services.creation,
        locks=rig.locks,
        activity=rig.recorder,
        clock=rig.runtime.clock,
        self_identity=None if identity is None else Held(identity),
        slots=ExportSlots() if slots is None else slots,
    )


def known_mounts(identity: SelfIdentity) -> SelfIdentity:
    return dataclasses.replace(identity, mount_sources={ENV_A: frozenset({"/srv/dockmin"})})


async def begin(
    service: ContainerExportService,
    *,
    digest: str | None = None,
    passphrase: str | None = PASSPHRASE,
    secrets: tuple[str, ...] = (SECRET_NAME,),
    volumes: tuple[str, ...] = (VOLUME,),
    container: str = SOURCE,
) -> ContainerExportStream:
    if digest is None:
        digest = (await service.confirmation(ADMIN, ENV_A, container)).digest
    return await service.export(ADMIN, ENV_A, container, digest, passphrase, secrets, volumes)


async def drain(stream: ContainerExportStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


async def test_an_export_holds_the_form_state_the_networks_the_image_and_the_chosen_volume(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)
    confirmation = await service.confirmation(ADMIN, ENV_A, SOURCE)

    archive = await drain(await begin(service))

    members = unpack(archive)
    manifest = manifest_of(members)
    assert manifest.container is not None
    environment = {entry.name: entry for entry in manifest.container.environment}
    (network,) = manifest.container.networks
    (image,) = manifest.images
    (volume,) = manifest.volumes
    assert (manifest.format_version, manifest.kind, manifest.name) == (1, "container", SOURCE_NAME)
    assert next(iter(members)) == "manifest.json"
    assert members[f"{volume.prefix}/data/{NOTE_FILE}"][1] == NOTE
    assert [v.name for v in confirmation.volumes] == [volume.manifest.name] == [VOLUME]
    assert environment[PLAIN_NAME].value == PLAIN_VALUE and environment[SECRET_NAME].secret
    assert environment[SECRET_NAME].value is None and manifest.secrets == "sealed"
    assert (network.name, network.driver, network.internal) == (NETWORK, "bridge", True)
    assert network.labels == {"team": "core"} and network.removed_option_keys == ["password"]
    assert (image.reference, image.tag, image.digest) == (IMAGE, "1", IMAGE_DIGEST)
    assert manifest.container.form["image"] == IMAGE
    assert not any(key.startswith("env-") for key in manifest.container.form)
    assert [(b.source, b.target, b.read_only) for b in confirmation.binds] == [
        (BIND_SOURCE, "/host", True)
    ]
    assert BIND_SOURCE in manifest.container.form.values()


async def test_a_marked_value_opens_under_the_passphrase_and_an_unmarked_one_is_plain(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    manifest = manifest_of(unpack(await drain(await begin(exporter(rig), volumes=()))))

    assert manifest.container is not None and manifest.sealed is not None
    position = [e.name for e in manifest.container.environment].index(SECRET_NAME)
    opened = open_sealed(manifest.sealed, PASSPHRASE, manifest.archive_id)
    assert opened == {f"environment:{position}": SECRET_VALUE.encode()}
    assert manifest.volumes == []


async def test_a_marked_value_is_left_out_when_no_passphrase_is_given(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    archive = await drain(await begin(exporter(rig), passphrase=None, volumes=()))

    members = unpack(archive)
    manifest = manifest_of(members)
    assert manifest.secrets == "omitted" and manifest.sealed is None
    assert SECRET_VALUE not in archive_text(members) and PLAIN_VALUE in archive_text(members)
    (finished,) = await rows_of(rig.db, "container_exported")
    assert finished["detail"]["secrets"] == "omitted"


async def test_no_secret_value_or_passphrase_reaches_the_archive_a_log_line_or_a_row(
    rig: ContainerRig, host: VolumeEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    service = exporter(rig)
    digest = (await service.confirmation(ADMIN, ENV_A, SOURCE)).digest
    archive = await drain(await begin(service, digest=digest))
    with pytest.raises(ValidationFailed):
        await begin(service, digest=digest, passphrase=WRONG_LENGTH)
    host.serve_volume(VOLUME, unterminated(volume_tar()))
    with pytest.raises(StreamMalformed):
        await drain(await begin(service, digest=digest))

    seen = [
        archive_text(unpack(archive)),
        json.dumps(await rows_of(rig.db)),
        logged_text(caplog),
        json.dumps(host.bodies()),
    ]
    assert [r["detail"].get("reason") for r in await rows_of(rig.db, "container_exported")] == [
        None,
        "ValidationFailed",
        "malformed_stream",
    ]
    assert PLAIN_VALUE in seen[0]
    for text in seen:
        assert SECRET_VALUE not in text and PASSPHRASE not in text and WRONG_LENGTH not in text
    assert NETWORK_PASSWORD not in seen[0] and host.helpers() == []


async def test_the_finished_row_names_counts_and_the_secrets_mode_and_no_values(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    archive = await drain(await begin(exporter(rig)))

    (row,) = await rows_of(rig.db, "container_exported")
    assert (row["result"], row["target_type"], row["target_id"]) == ("success", "container", SOURCE)
    detail = row["detail"]
    assert (detail["secrets"], detail["format_version"], detail["secret_values"]) == (
        "sealed",
        1,
        1,
    )
    assert (detail["volumes"], detail["networks"], detail["bind_mounts"]) == (1, 1, 1)
    assert (detail["completed"], detail["bytes"]) == (True, len(archive))
    assert host.helpers() == []


async def test_a_container_that_changed_since_the_review_is_refused_before_a_helper_exists(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    with pytest.raises(ContainerChangedSinceReview):
        await begin(exporter(rig), digest=STALE_DIGEST)

    (row,) = await rows_of(rig.db, "container_exported")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "changed_since_review")
    assert "create" not in host.verbs()


async def test_dockmins_own_container_is_refused_by_the_confirmation_and_the_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add(SELF_ID, "dockmin", image=IMAGE)
    service = exporter(rig, known_mounts(home_identity()))

    with pytest.raises(SelfProtected):
        await service.confirmation(ADMIN, ENV_A, SELF_ID)
    with pytest.raises(SelfProtected):
        await service.export(ADMIN, ENV_A, SELF_ID, STALE_DIGEST, None, (), ())

    (row,) = await rows_of(rig.db, "container_exported")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_every_export_is_refused_while_dockmins_own_mounts_are_unknown(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig, home_identity())

    with pytest.raises(SelfProtected):
        await service.confirmation(ADMIN, ENV_A, SOURCE)
    with pytest.raises(SelfProtected):
        await service.export(ADMIN, ENV_A, SOURCE, STALE_DIGEST, None, (), ())

    (row,) = await rows_of(rig.db, "container_exported")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_a_standard_user_is_refused_without_a_row_or_a_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)

    with pytest.raises(PermissionDenied):
        await service.confirmation(STANDARD, ENV_A, SOURCE)
    with pytest.raises(PermissionDenied):
        await service.export(STANDARD, ENV_A, SOURCE, STALE_DIGEST, None, (), ())

    assert await rows_of(rig.db, "container_exported") == []
    assert "create" not in host.verbs()


async def test_a_container_that_is_not_there_is_not_found(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    ghost = "f" * 64

    with pytest.raises(ResourceNotFound):
        await exporter(rig).confirmation(ADMIN, ENV_A, ghost)


async def test_a_plugin_volume_and_dockmins_own_volume_are_refused_on_confirmation_and_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("plugged", driver="nfs-plugin")
    host.add_volume("dockmin-data")
    host.add_configured(
        "e" * 64,
        "mixed",
        {
            "Image": IMAGE,
            "HostConfig": {"Binds": ["plugged:/plug", "dockmin-data:/own", f"{VOLUME}:/data"]},
        },
    )
    service = exporter(rig, known_mounts(home_identity(volumes=("dockmin-data",))))
    confirmation = await service.confirmation(ADMIN, ENV_A, "e" * 64)

    with pytest.raises(ContainerExportRefused) as plugin:
        await begin(service, container="e" * 64, secrets=(), volumes=("plugged",))
    with pytest.raises(SelfProtected):
        await begin(service, container="e" * 64, secrets=(), volumes=("dockmin-data",))

    refusals = {v.name: v.refusal is not None for v in confirmation.volumes}
    assert refusals == {"plugged": True, "dockmin-data": True, VOLUME: False}
    assert plugin.value.reason == "plugin_driver" and host.helpers() == []


async def test_a_secret_mark_or_volume_the_container_does_not_have_is_refused(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("elsewhere")
    service = exporter(rig)

    with pytest.raises(ValidationFailed):
        await begin(service, secrets=("NOT_A_VARIABLE",), volumes=())
    with pytest.raises(ValidationFailed):
        await begin(service, secrets=(), volumes=("elsewhere",))
    with pytest.raises(ValidationFailed):
        await begin(service, secrets=(), volumes=("../escape",))

    assert "create" not in host.verbs()


async def test_a_host_with_no_image_is_refused_before_a_helper_is_created(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)
    host.image_store.clear()
    host.images.clear()
    refused = await service.confirmation(ADMIN, ENV_A, SOURCE)

    with pytest.raises(ContainerExportRefused) as missing:
        await begin(service, digest=refused.digest)

    assert refused.helper_image is None and not refused.can_copy_volumes
    assert missing.value.reason == "helper_image_missing" and "create" not in host.verbs()


async def test_a_second_export_on_the_environment_is_refused_until_the_first_ends(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    service = exporter(rig)
    first = await begin(service, volumes=())

    with pytest.raises(ContainerExportBusy):
        await begin(service, volumes=())
    await drain(first)
    await drain(await begin(service, volumes=()))

    busy = [r for r in await rows_of(rig.db, "container_exported") if r["result"] == "failure"]
    assert [r["detail"]["reason"] for r in busy] == ["export_busy"]


async def test_a_client_that_leaves_part_way_ends_the_export_and_releases_what_it_held(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({"big": b"x" * (1 << 20)}))
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots))
    await stream.__anext__()
    await asyncio.sleep(0)

    await stream.aclose()
    await stream.aclose()

    (finished,) = await rows_of(rig.db, "container_exported")
    assert (finished["result"], finished["detail"]["reason"]) == ("failure", "interrupted")
    assert host.helpers() == [] and slots.acquire(ENV_A) is not None
