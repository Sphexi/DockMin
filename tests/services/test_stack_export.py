"""StackExportService over a fake daemon and the seeded database."""

import dataclasses
import json
import logging
from collections.abc import Iterator

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, SelfProtected, ValidationFailed
from app.db.repositories.stack import StackRepository, StoredStack, VariableWrite
from app.self_identity import SelfIdentity
from app.services.activity import UserActor
from app.services.export_manifest import ImageReference, parse_manifest
from app.services.export_sealing import MINIMUM_COST, SealError, open_sealed
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_export import (
    StackChangedSinceReview,
    StackExportBusy,
    StackExportRefused,
    StackExportService,
    StackExportStream,
    review_digest,
)
from app.services.volume_export import ExportSlots
from app.services.volume_export_writer import StreamMalformed
from tests.access_support import ADMIN, ENV_A, STANDARD, container_id
from tests.object_support import add_container, rows_of, volume_mount
from tests.self_support import SELF_VOLUME, Held, home_identity
from tests.services.conftest import ContainerRig
from tests.stack_export_support import (
    COMPOSE_LABEL,
    COMPOSE_TEXT,
    CONTEXT,
    PASSPHRASE,
    PLAIN_FILE,
    PLAIN_FILE_BODY,
    PLAIN_NAME,
    PLAIN_VALUE,
    SECRET_FILE,
    SECRET_FILE_BODY,
    SECRET_NAME,
    SECRET_VALUE,
    STACK,
    SWARM_LABEL,
    VOLUME,
    WEB_DIGEST,
    WEB_IMAGE,
    SwarmVolumeEngine,
    seed_export_stack,
    stocked,
)
from tests.volume_export_support import (
    NOTE,
    NOTE_FILE,
    VolumeEngine,
    unpack,
    unterminated,
    volume_tar,
)

OTHER_SECRET = "replacement-secret-value-1182"
SHORT_PASSPHRASE = "too short"
KEY = ObjectKey(ENV_A, "stack", STACK)


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[SwarmVolumeEngine]:
    daemon = stocked(SwarmVolumeEngine())
    yield daemon
    daemon.release_gates()


def exporter(
    rig: ContainerRig, identity: SelfIdentity | None = None, slots: ExportSlots | None = None
) -> StackExportService:
    return StackExportService(
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        stacks=StackRepository(rig.db, rig.engine.vault, rig.runtime.clock),
        locks=rig.locks,
        activity=rig.recorder,
        clock=rig.runtime.clock,
        self_identity=None if identity is None else Held(identity),
        slots=ExportSlots() if slots is None else slots,
        seal_cost=MINIMUM_COST,
    )


async def seeded(rig: ContainerRig, **options: object) -> StoredStack:
    return await seed_export_stack(rig.db, rig.engine.vault, rig.runtime.clock, **options)  # type: ignore[arg-type]


async def begin(
    service: StackExportService,
    stack: StoredStack,
    *,
    passphrase: str | None = PASSPHRASE,
    volumes: tuple[str, ...] = (VOLUME,),
    digest: str | None = None,
) -> StackExportStream:
    return await service.export(
        ADMIN,
        stack.environment_id,
        stack.kind,
        stack.name,
        digest or review_digest(stack),
        passphrase,
        volumes,
    )


async def drain(stream: StackExportStream) -> bytes:
    try:
        return b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()


async def make_swarm(rig: ContainerRig) -> None:
    await rig.db.execute("UPDATE environment SET type = 'swarm' WHERE id = ?", (ENV_A,))


async def test_a_compose_stack_exports_into_the_layout_the_confirmation_lists(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    members = unpack(await drain(await begin(service, stack)))

    manifest = parse_manifest(members["manifest.json"][1])
    assert next(iter(members)) == "manifest.json"
    assert {name for name in members if not name.startswith("volumes/")} == set(
        confirmation.members
    )
    assert (manifest.format_version, manifest.kind, manifest.secrets) == (
        1,
        "compose_stack",
        "sealed",
    )
    assert members["stack/compose.yaml"][1].decode() == COMPOSE_TEXT
    assert members[f"stack/files/{PLAIN_FILE}"][1].decode() == PLAIN_FILE_BODY
    assert members["stack/contexts/0"][1] and manifest.stack is not None
    assert manifest.stack.contexts[0].name == CONTEXT
    assert [(v.name, v.secret, v.value) for v in manifest.stack.variables] == [
        (SECRET_NAME, True, None),
        (PLAIN_NAME, False, PLAIN_VALUE),
    ]
    assert members[f"volumes/0/data/{NOTE_FILE}"][1] == NOTE
    assert manifest.volumes[0].manifest.name == VOLUME
    assert manifest.images == [
        ImageReference(reference=WEB_IMAGE, tag="1.27", digest=WEB_DIGEST, used_by=["web"])
    ]
    assert confirmation.images == tuple(manifest.images)


async def test_sealed_secrets_open_with_the_passphrase_only(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)

    members = unpack(await drain(await begin(exporter(rig), stack, volumes=())))

    manifest = parse_manifest(members["manifest.json"][1])
    assert manifest.sealed is not None
    opened = open_sealed(manifest.sealed, PASSPHRASE, manifest.archive_id)
    assert opened == {
        f"variable:{SECRET_NAME}": SECRET_VALUE.encode(),
        f"file:{SECRET_FILE}": SECRET_FILE_BODY.encode(),
    }
    with pytest.raises(SealError):
        open_sealed(manifest.sealed, PASSPHRASE + "x", manifest.archive_id)
    assert f"stack/files/{SECRET_FILE}" not in members and "create" not in host.verbs()


async def test_leaving_secrets_out_never_opens_them_and_lists_names_only(
    rig: ContainerRig, host: VolumeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = await seeded(rig)

    def refuse(*_: object) -> None:
        raise AssertionError("a secret was opened")

    monkeypatch.setattr(StackRepository, "secret_variable_opener", refuse)
    members = unpack(await drain(await begin(exporter(rig), stack, passphrase=None, volumes=())))

    manifest = parse_manifest(members["manifest.json"][1])
    assert (manifest.secrets, manifest.sealed) == ("omitted", None)
    assert manifest.stack is not None
    assert [f.name for f in manifest.stack.files if f.secret] == [SECRET_FILE]
    assert not any(SECRET_VALUE.encode() in data for _, data in members.values())


async def test_a_started_row_precedes_the_bytes_and_a_finished_row_records_the_counts(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    stream = await begin(exporter(rig), stack)

    before = [(r["action"], r["result"]) for r in await rows_of(rig.db)]
    archive = await drain(stream)
    started, finished = await rows_of(rig.db)

    assert before == [("stack_export_started", "success")]
    assert (started["target_type"], started["target_id"], started["environment_id"]) == (
        "stack",
        STACK,
        ENV_A,
    )
    assert (finished["action"], finished["result"]) == ("stack_exported", "success")
    assert finished["detail"] | {"bytes": 0} == {
        "name": STACK,
        "stack_kind": "compose",
        "variables": 2,
        "secret_values": 2,
        "files": 2,
        "contexts": 1,
        "volumes": 1,
        "secrets": "sealed",
        "format_version": 1,
        "bytes": 0,
        "volumes_done": 1,
        "skipped": 0,
        "completed": True,
        "helpers_removed": True,
    }
    assert finished["detail"]["bytes"] == len(archive)
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None


async def test_a_running_stack_is_named_and_exported_without_stopping_anything(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    add_container(
        host,
        1,
        "shop-web-1",
        state="running",
        labels={COMPOSE_LABEL: STACK},
        mounts=[volume_mount(VOLUME)],
    )
    stack = await seeded(rig)
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    members = unpack(await drain(await begin(service, stack)))

    assert [user.name for user in confirmation.running] == ["shop-web-1"]
    assert [(v.name, v.running, v.selectable) for v in confirmation.volumes] == [(VOLUME, 1, True)]
    assert members[f"volumes/0/data/{NOTE_FILE}"][1] == NOTE
    assert {"stop", "kill", "restart"}.isdisjoint(host.verbs())


async def test_host_paths_are_listed_and_kept_as_written(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    members = unpack(await drain(await begin(service, stack, volumes=())))

    assert confirmation.host_paths == ("./site",)
    assert "./site:/usr/share/nginx/html" in members["stack/compose.yaml"][1].decode()


async def test_a_swarm_stack_exports_with_the_volumes_of_its_namespace(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.volume_labels[VOLUME] = {SWARM_LABEL: STACK}
    await make_swarm(rig)
    stack = await seeded(rig, kind="swarm")
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, "swarm", STACK)
    members = unpack(await drain(await begin(service, stack)))

    manifest = parse_manifest(members["manifest.json"][1])
    assert [v.name for v in confirmation.volumes] == [VOLUME]
    assert manifest.stack is not None
    assert (manifest.kind, manifest.stack.stack_kind) == ("swarm_stack", "swarm")
    assert members[f"volumes/0/data/{NOTE_FILE}"][1] == NOTE


async def test_a_standard_user_is_refused_without_a_row_or_a_helper(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    service = exporter(rig)

    with pytest.raises(PermissionDenied):
        await service.confirmation(STANDARD, ENV_A, "compose", STACK)
    with pytest.raises(PermissionDenied):
        await service.export(STANDARD, ENV_A, "compose", STACK, review_digest(stack), None, ())

    assert await rows_of(rig.db) == [] and "create" not in host.verbs()


async def test_a_missing_stack_or_one_of_the_other_kind_is_not_found(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    service = exporter(rig)

    for kind, name in (("compose", "missing"), ("compose", "../escape"), ("swarm", STACK)):
        with pytest.raises(ResourceNotFound):
            await service.export(ADMIN, ENV_A, kind, name, review_digest(stack), None, ())  # type: ignore[arg-type]

    assert await rows_of(rig.db) == []


async def test_dockmins_own_stack_is_refused_before_anything_is_read(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    identity = dataclasses.replace(
        home_identity(compose_project=STACK), mount_sources={ENV_A: frozenset()}
    )
    service = exporter(rig, identity)

    with pytest.raises(SelfProtected):
        await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    with pytest.raises(SelfProtected):
        await begin(service, stack)

    (row,) = await rows_of(rig.db, "stack_export_started")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_a_stack_holding_dockmins_container_is_refused(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    add_container(host, 1, "dockmin", state="running", labels={COMPOSE_LABEL: STACK})
    own = dataclasses.replace(
        home_identity(container=container_id(1)), mount_sources={ENV_A: frozenset()}
    )

    with pytest.raises(SelfProtected):
        await begin(exporter(rig, own), stack)


async def test_every_export_is_refused_while_dockmins_own_mounts_are_unknown(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig, name="blog")
    service = exporter(rig, home_identity())

    with pytest.raises(SelfProtected):
        await service.confirmation(ADMIN, ENV_A, "compose", "blog")
    with pytest.raises(SelfProtected):
        await begin(service, stack)

    (row,) = await rows_of(rig.db, "stack_export_started")
    assert row["detail"]["reason"] == "self_protected" and "create" not in host.verbs()


async def test_a_stack_that_changed_since_the_review_is_refused_and_leaves_nothing_held(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    reviewed = review_digest(stack)
    await StackRepository(rig.db, rig.engine.vault, rig.runtime.clock).replace_variables(
        stack.id, [VariableWrite(PLAIN_NAME, False, "edited")]
    )
    slots = ExportSlots()

    with pytest.raises(StackChangedSinceReview):
        await begin(exporter(rig, slots=slots), stack, digest=reviewed)

    (row,) = await rows_of(rig.db, "stack_export_started")
    assert row["detail"]["reason"] == "changed_since_review"
    assert "create" not in host.verbs() and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None


async def test_a_replaced_secret_changes_the_review_digest(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    repository = StackRepository(rig.db, rig.engine.vault, rig.runtime.clock)
    await repository.replace_variables(
        stack.id,
        [
            VariableWrite(PLAIN_NAME, False, PLAIN_VALUE),
            VariableWrite(SECRET_NAME, True, OTHER_SECRET),
        ],
    )
    changed = await repository.get_by_id(stack.id)

    assert changed is not None and review_digest(changed) != review_digest(stack)


async def test_a_plugin_driver_a_foreign_name_and_dockmins_own_volume_are_refused(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.add_volume("plugged", driver="nfs-plugin", labels={COMPOSE_LABEL: STACK})
    host.add_volume(SELF_VOLUME, labels={COMPOSE_LABEL: STACK})
    stack = await seeded(rig)
    identity = dataclasses.replace(
        home_identity(volumes=(SELF_VOLUME,)), mount_sources={ENV_A: frozenset({"/srv"})}
    )
    service = exporter(rig, identity)

    confirmation = await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    with pytest.raises(StackExportRefused) as plugin:
        await begin(service, stack, volumes=("plugged",))
    with pytest.raises(SelfProtected):
        await begin(service, stack, volumes=(SELF_VOLUME,))
    with pytest.raises(ValidationFailed):
        await begin(service, stack, volumes=("elsewhere",))

    assert {v.name: v.selectable for v in confirmation.volumes} == {
        "plugged": False,
        SELF_VOLUME: False,
        VOLUME: True,
    }
    assert plugin.value.reason == "plugin_driver"
    assert "create" not in host.verbs() and rig.locks.holder_of(KEY) is None


async def test_a_busy_stack_and_a_full_slot_refuse_the_export(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    stack = await seeded(rig)
    holder = LockHolder(UserActor.of(ADMIN), ADMIN.username, "redeploy")

    async with rig.locks.hold(KEY, holder):
        with pytest.raises(ObjectBusy):
            await begin(exporter(rig), stack)
    with pytest.raises(StackExportBusy):
        await begin(exporter(rig, slots=ExportSlots(per_environment=0)), stack)

    reasons = [r["detail"]["reason"] for r in await rows_of(rig.db, "stack_export_started")]
    assert reasons == ["object_busy", "export_busy"] and "create" not in host.verbs()


async def test_a_host_with_no_image_cannot_export_volumes_but_exports_the_rest(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.image_store.clear()
    host.images.clear()
    stack = await seeded(rig)
    service = exporter(rig)

    confirmation = await service.confirmation(ADMIN, ENV_A, "compose", STACK)
    with pytest.raises(StackExportRefused) as refused:
        await begin(service, stack)
    members = unpack(await drain(await begin(service, stack, volumes=())))

    assert confirmation.helper_image is None and not confirmation.can_export_volumes
    assert refused.value.reason == "helper_image_missing" and "stack/compose.yaml" in members


async def test_a_client_that_leaves_part_way_ends_the_export_and_records_it(
    rig: ContainerRig, host: VolumeEngine
) -> None:
    host.serve_volume(VOLUME, volume_tar({"big": b"x" * (1 << 20)}))
    stack = await seeded(rig)
    slots = ExportSlots()
    stream = await begin(exporter(rig, slots=slots), stack)
    await stream.__anext__()

    await stream.aclose()
    await stream.aclose()

    (finished,) = await rows_of(rig.db, "stack_exported")
    assert (finished["result"], finished["detail"]["reason"]) == ("failure", "interrupted")
    assert finished["detail"]["completed"] is False
    assert host.helpers() == [] and rig.locks.holder_of(KEY) is None
    assert slots.acquire(ENV_A) is not None


async def test_nothing_secret_reaches_the_archive_a_log_line_or_a_row_on_any_path(
    rig: ContainerRig, host: VolumeEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    stack = await seeded(rig)
    service = exporter(rig)
    raw: list[bytes] = []

    sealed = await drain(await begin(service, stack))
    omitted = await drain(await begin(service, stack, passphrase=None))
    raw += [sealed, omitted]
    with pytest.raises(ValidationFailed):
        await begin(service, stack, passphrase=SHORT_PASSPHRASE)
    host.serve_volume(VOLUME, unterminated(volume_tar()))
    with pytest.raises(StreamMalformed):
        await drain(await begin(service, stack))

    texts = [caplog.text, json.dumps(await rows_of(rig.db), default=str)]
    for secret in (SECRET_VALUE, SECRET_FILE_BODY.strip(), PASSPHRASE, SHORT_PASSPHRASE):
        assert not [t for t in texts if secret in t], secret
        assert not [a for a in raw if secret.encode() in _expanded(a)], secret
    reasons = [r["detail"].get("reason") for r in await rows_of(rig.db) if r["result"] == "failure"]
    assert reasons == ["invalid_passphrase", "malformed_stream"]


def _expanded(archive: bytes) -> bytes:
    return b"".join(data for _, data in unpack(archive).values()) + archive
