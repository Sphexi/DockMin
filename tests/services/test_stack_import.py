"""StackImportService over a fake daemon, the scripted Compose and the seeded database."""

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.services import AuthRuntime
from app.crypto.vault import Vault
from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_dismissal import StackDismissalRepository
from app.services.activity import UserActor
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.pull_credentials import CredentialChoice
from app.services.stack_deploy import PreexistingObjects, StackRejected, file_digest
from app.services.stack_import import (
    ImportBlocked,
    StackImportChanged,
    StackImported,
    StackImportFailed,
    StackImportService,
    StackUploadGone,
    UnlockFailed,
    UnlockLimited,
)
from app.services.stack_import_plan import ImportChoices
from app.services.stack_import_store import UNSEAL_FAILURE_LIMIT
from app.services.stacks import StackService
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import FakeClock
from tests.export_archive_support import (
    FIXTURE,
    FIXTURE_PASSPHRASE,
    PASSPHRASE,
    PLAIN_VALUE,
    SECRET_VALUE,
)
from tests.image_support import REFERENCE, binding_rows, register
from tests.stack_import_support import (
    ADMIN_SESSION,
    BIND_STACK_FILE,
    DATA_VOLUME,
    NOTE,
    NOTE_FILE,
    OTHER_ADMIN_SESSION,
    OTHER_SESSION,
    PLAIN_FILE,
    PLAIN_FILE_BODY,
    PLAIN_NAME,
    SECRET_FILE,
    SECRET_FILE_BODY,
    SECRET_NAME,
    SESSION,
    SOURCE,
    STACK_FILE,
    WRONG_PASSPHRASE,
    StackImportEngine,
    build_archive,
    build_importer,
    parts,
)
from tests.stack_support import Rig, activity_dump, logged_text
from tests.swarm_stack_support import StackDaemon, build_swarm_rig

COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_VOLUME = "com.docker.compose.volume"
RENAMED = "copy"


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[StackImportEngine]:
    daemon = StackImportEngine()
    yield daemon
    daemon.release_gates()


async def uploaded(
    service: StackImportService, archive: bytes | None = None, *, kind: str = "compose"
) -> str:
    data = archive if archive is not None else await build_archive()
    return await service.receive(ADMIN_SESSION, SESSION, ENV_A, kind, parts(data))  # type: ignore[arg-type]


async def opened(service: StackImportService, upload_id: str, passphrase: str = PASSPHRASE) -> None:
    await service.unlock(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id, passphrase)


async def confirm(
    service: StackImportService,
    upload_id: str,
    *,
    name: str = SOURCE,
    host_paths: dict[str, str] | None = None,
    credentials: dict[str, CredentialChoice] | None = None,
    entered: dict[str, str] | None = None,
    kind: str = "compose",
) -> StackImported:
    choices = ImportChoices(
        name=name,
        host_paths=host_paths or {},
        credentials=credentials or {},
        entered=entered or {},
    )
    review = await service.review(
        ADMIN_SESSION,
        SESSION,
        ENV_A,
        kind,  # type: ignore[arg-type]
        upload_id,
        choices,
    )
    return await service.import_stack(
        ADMIN_SESSION,
        ENV_A,
        kind,  # type: ignore[arg-type]
        upload_id,
        choices,
        review.plan.digest,
    )


async def stored(rig: Rig, name: str = SOURCE) -> StackRepository:
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    assert await repository.get(ENV_A, name) is not None
    return repository


async def test_a_compose_archive_imports_its_file_variables_files_and_volume_data(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)

    imported = await confirm(service, upload_id)

    repository = await stored(rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None
    assert row.compose_file == STACK_FILE and row.compose_file_digest == file_digest(STACK_FILE)
    variables = {item.name: item for item in await repository.variables(row.id)}
    assert variables[PLAIN_NAME].value == PLAIN_VALUE
    assert variables[SECRET_NAME].is_secret and variables[SECRET_NAME].value is None
    assert (await repository.reveal_secrets(row.id))[SECRET_NAME] == SECRET_VALUE
    files = {item.name: item for item in await repository.files.files(row.id)}
    assert files[PLAIN_FILE].content == PLAIN_FILE_BODY.decode()
    assert (await repository.files.reveal_files(row.id))[SECRET_FILE] == SECRET_FILE_BODY
    assert imported.volumes == (DATA_VOLUME,) and imported.bytes_written > 0
    assert host.volume_labels[DATA_VOLUME] == {COMPOSE_PROJECT: SOURCE, COMPOSE_VOLUME: "data"}
    assert host.files_of(DATA_VOLUME)[NOTE_FILE].data == NOTE
    assert host.helpers() == []
    control = await rig.db.fetch_one(
        "SELECT owner_user_id FROM resource_control WHERE resource_type = 'stack' "
        "AND resource_id = ?",
        (SOURCE,),
    )
    assert control is not None and control["owner_user_id"] == ADMIN.user_id
    [event] = await rig.rows("stack_imported")
    assert event["result"] == "success" and event["target_id"] == SOURCE
    assert event["detail"]["volumes_created"] == 1
    assert event["detail"]["variables"] == 2 and event["detail"]["format_version"] == 1
    assert SECRET_VALUE not in json.dumps(event["detail"])


async def test_secrets_open_only_with_the_right_passphrase_and_wrong_ones_are_counted(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    upload_id = await uploaded(service)

    for attempt in range(UNSEAL_FAILURE_LIMIT):
        with pytest.raises(UnlockFailed) as failed:
            await opened(service, upload_id, WRONG_PASSPHRASE)
        assert failed.value.remaining == UNSEAL_FAILURE_LIMIT - attempt - 1
    with pytest.raises(UnlockLimited):
        await opened(service, upload_id)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)

    assert not review.opened and review.unlock_remaining == 0
    assert {item.state for item in review.plan.secrets} == {"needed"}
    with pytest.raises(ImportBlocked) as blocked:
        await confirm(service, upload_id)
    assert blocked.value.reason == "secrets_needed"
    assert host.volumes == {} and await rig_rows(stack_rig, "stack_imported", "failure")


async def rig_rows(rig: Rig, action: str, result: str) -> list[dict[str, object]]:
    return [row for row in await rig.rows(action) if row["result"] == result]


async def test_entered_values_stand_in_for_a_sealed_archive_and_are_stored_encrypted(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    upload_id = await uploaded(service)

    await confirm(
        service,
        upload_id,
        entered={"variable:DB_PASSWORD": "typed-secret-value", "file:tls.key": "typed-file-value"},
    )

    repository = await stored(stack_rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None
    assert (await repository.reveal_secrets(row.id))[SECRET_NAME] == "typed-secret-value"
    assert (await repository.files.reveal_files(row.id))[SECRET_FILE] == "typed-file-value"


async def test_a_session_that_did_not_upload_cannot_review_unlock_or_import(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    upload_id = await uploaded(service)

    with pytest.raises(StackUploadGone):
        await service.review(OTHER_ADMIN_SESSION, OTHER_SESSION, ENV_A, "compose", upload_id)
    with pytest.raises(StackUploadGone):
        await service.unlock(OTHER_ADMIN_SESSION, OTHER_SESSION, ENV_A, "compose", upload_id, "x")
    with pytest.raises(StackUploadGone):
        await service.import_stack(
            OTHER_ADMIN_SESSION,
            ENV_A,
            "compose",
            upload_id,
            ImportChoices(name=SOURCE),
            "any",
        )
    [failure] = await rig_rows(stack_rig, "stack_imported", "failure")
    assert failure["detail"]["reason"] == "upload_gone"  # type: ignore[index]


async def test_a_standard_user_is_refused_everywhere_and_no_row_is_written(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    standard = dataclasses.replace(STANDARD, session_id=SESSION)
    archive = await build_archive()

    with pytest.raises(PermissionDenied):
        await service.receive(standard, SESSION, ENV_A, "compose", parts(archive))
    with pytest.raises(PermissionDenied):
        await service.review(standard, SESSION, ENV_A, "compose", "0" * 32)
    with pytest.raises(PermissionDenied):
        await service.import_stack(
            standard, ENV_A, "compose", "0" * 32, ImportChoices(name=SOURCE), "any"
        )

    assert await stack_rig.rows("stack_imported") == []
    assert host.volumes == {}


async def test_a_rename_renames_every_project_derived_name(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)

    review = await service.review(
        ADMIN_SESSION,
        SESSION,
        ENV_A,
        "compose",
        upload_id,
        ImportChoices(name=RENAMED),
    )
    imported = await confirm(service, upload_id, name=RENAMED)

    names = {(item.kind, item.name) for item in review.plan.objects}
    assert names == {
        ("container", f"{RENAMED}-web-1"),
        ("network", f"{RENAMED}_default"),
        ("volume", f"{RENAMED}_data"),
    }
    assert imported.name == RENAMED and imported.volumes == (f"{RENAMED}_data",)
    assert host.volume_labels[f"{RENAMED}_data"][COMPOSE_PROJECT] == RENAMED
    assert host.files_of(f"{RENAMED}_data")[NOTE_FILE].data == NOTE
    await stored(stack_rig, RENAMED)


async def test_every_collision_is_named_and_refuses_until_the_name_changes(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    host.add_volume("shop_data")
    host.add_network("shop_default", 0xAB0)
    host.add("c" * 64, "shop-web-1", labels={})

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)

    kinds = {(item.kind, item.name) for item in review.plan.collisions}
    assert {("volume", "shop_data"), ("network", "shop_default")} <= kinds
    assert not review.can_import
    with pytest.raises(ImportBlocked) as blocked:
        await confirm(service, upload_id)
    assert blocked.value.reason == "collision"

    imported = await confirm(service, upload_id, name=RENAMED)

    assert imported.name == RENAMED
    assert host.volume_removals() == []


async def test_a_leftover_labelled_volume_and_a_dismissed_project_hold_the_name(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    host.add_volume("old_cache", labels={COMPOSE_PROJECT: SOURCE})

    leftover = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)

    assert [(item.kind, item.name) for item in leftover.plan.collisions] == [("stack", SOURCE)]
    await StackDismissalRepository(rig.db, rig.clock).put_dismissal(
        ENV_A, "compose", SOURCE, ["volume:old_cache"], None
    )
    dismissed = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    assert [item.kind for item in dismissed.plan.collisions] == ["stack"]

    host.volumes.pop("old_cache")
    host.volume_labels.pop("old_cache")
    imported = await confirm(service, upload_id)

    assert imported.name == SOURCE
    gone = await StackDismissalRepository(rig.db, rig.clock).dismissal(ENV_A, "compose", SOURCE)
    assert gone is None


def host_bind_sources(host: StackImportEngine) -> list[str]:
    sources: list[str] = []
    for call in host.calls("POST", "/containers/create"):
        body = json.loads(call.body)
        sources.extend(str(bind).split(":", 1)[0] for bind in body["HostConfig"].get("Binds") or [])
        sources.extend(
            str(mount.get("Source", ""))
            for mount in body["HostConfig"].get("Mounts") or []
            if mount.get("Type") == "bind"
        )
    return sources


async def test_host_paths_need_a_choice_and_a_kept_path_leaves_the_file_as_archived(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    archive = await build_archive(text=BIND_STACK_FILE, volumes={})
    upload_id = await uploaded(service, archive)
    await opened(service, upload_id)

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)

    assert [(item.source, item.uses) for item in review.plan.host_paths] == [
        ("/srv/logs", ("web",)),
        ("/srv/site", ("web",)),
    ]
    with pytest.raises(ImportBlocked) as blocked:
        await confirm(service, upload_id)
    assert blocked.value.reason == "paths_undecided"

    await confirm(service, upload_id, host_paths={"/srv/logs": "keep", "/srv/site": "keep"})

    repository = await stored(stack_rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None and row.compose_file == BIND_STACK_FILE
    assert host_bind_sources(host) == []
    assert host.volumes == {}


async def test_a_replaced_host_path_becomes_a_named_volume_and_no_directory_is_made(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    archive = await build_archive(text=BIND_STACK_FILE, volumes={})
    upload_id = await uploaded(service, archive)
    await opened(service, upload_id)

    review = await service.review(
        ADMIN_SESSION,
        SESSION,
        ENV_A,
        "compose",
        upload_id,
        ImportChoices(name=SOURCE, host_paths={"/srv/logs": "keep", "/srv/site": "volume"}),
    )
    await confirm(service, upload_id, host_paths={"/srv/logs": "keep", "/srv/site": "volume"})

    repository = await stored(stack_rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None
    assert "/srv/site" not in row.compose_file and "/srv/logs" in row.compose_file
    assert "hostpath-1:/usr/share/nginx/html:ro" in row.compose_file
    assert "hostpath-1" in row.compose_file.split("volumes:")[-1]
    assert [item.volume for item in review.plan.host_paths] == [None, "hostpath-1"]
    assert host_bind_sources(host) == [] and host.volumes == {}


async def test_the_deploy_accepts_only_the_volumes_this_import_created(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    await confirm(service, upload_id)
    assert rig.engine.imported_volumes.registered(ENV_A, SOURCE)

    host.volume_created[DATA_VOLUME] = "2001-01-01T00:00:00Z"
    with pytest.raises(PreexistingObjects) as refused:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, SOURCE)
    assert refused.value.objects == (f"Volume {DATA_VOLUME}",)

    host.volume_created[DATA_VOLUME] = next(iter(rig.engine.imported_volumes._volumes.values()))[
        DATA_VOLUME
    ]
    outcome = await rig.fresh().actions.deploy(ADMIN, ENV_A, SOURCE)

    assert outcome.services_running if hasattr(outcome, "services_running") else outcome
    assert not rig.engine.imported_volumes.registered(ENV_A, SOURCE)
    assert host.volume_labels[DATA_VOLUME][COMPOSE_PROJECT] == SOURCE


async def test_a_volume_written_before_the_deploy_is_the_stacks_own_for_a_plain_deploy_too(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    await confirm(service, upload_id)
    rig.engine.imported_volumes.release(ENV_A, SOURCE)

    await rig.fresh().actions.deploy(ADMIN, ENV_A, SOURCE)

    assert host.files_of(DATA_VOLUME)[NOTE_FILE].data == NOTE
    assert [box.labels.get(COMPOSE_PROJECT) for box in host.boxes.values()] == [SOURCE]


async def test_a_failed_write_removes_the_volumes_this_import_created_and_names_them(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    archive = await build_archive(
        text=STACK_FILE.replace("volumes:\n  data: {}", "volumes:\n  data: {}\n  logs: {}").replace(
            "      - data:/var/lib/data", "      - data:/var/lib/data\n      - logs:/var/log"
        ),
        volumes={DATA_VOLUME: {NOTE_FILE: NOTE}, "shop_logs": {"a.log": b"1\n", "b.log": b"2\n"}},
    )
    upload_id = await uploaded(service, archive)
    await opened(service, upload_id)
    host.fail_after_members = 1

    with pytest.raises(StackImportFailed) as failed:
        await confirm(service, upload_id)

    assert failed.value.kept == () and failed.value.removed == ("shop_logs", "shop_data")
    assert host.volumes == {}
    assert host.helpers() == []
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    assert await repository.get(ENV_A, SOURCE) is None
    assert not rig.engine.imported_volumes.registered(ENV_A, SOURCE)
    [row] = await rig_rows(rig, "stack_imported", "failure")
    assert row["detail"]["volumes_removed"] == "shop_logs,shop_data"  # type: ignore[index]
    assert row["detail"]["reason"] == "engine_error"  # type: ignore[index]


async def test_a_changed_plan_is_refused_before_anything_is_created(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    host.add_network("shop_default", 0xAB1)

    with pytest.raises(StackImportChanged):
        await service.import_stack(
            ADMIN_SESSION,
            ENV_A,
            "compose",
            upload_id,
            ImportChoices(name=SOURCE),
            review.plan.digest,
        )

    assert host.volumes == {}
    [row] = await rig_rows(rig, "stack_imported", "failure")
    assert row["detail"]["reason"] == "changed_since_review"  # type: ignore[index]


async def test_choosing_host_paths_credentials_or_values_does_not_change_the_plan_digest(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    archive = await build_archive(text=BIND_STACK_FILE, volumes={})
    upload_id = await uploaded(service, archive)

    plain = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    chosen = await service.review(
        ADMIN_SESSION,
        SESSION,
        ENV_A,
        "compose",
        upload_id,
        ImportChoices(
            name=SOURCE,
            host_paths={"/srv/site": "volume", "/srv/logs": "keep"},
            entered={"variable:DB_PASSWORD": "typed"},
        ),
    )
    renamed = await service.review(
        ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id, ImportChoices(name=RENAMED)
    )

    assert plain.plan.digest == chosen.plan.digest != renamed.plan.digest


async def test_an_image_is_pulled_through_the_chosen_set_and_bound(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    set_id = await register(rig, "only", environments=(ENV_A,))
    service, _ = build_importer(rig)
    archive = await build_archive(text=f"services:\n  app:\n    image: {REFERENCE}\n", volumes={})
    upload_id = await uploaded(service, archive)
    await opened(service, upload_id)

    with pytest.raises(StackRejected):
        await confirm(service, upload_id)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    assert review.pull_references == (REFERENCE,)

    choice = {REFERENCE: CredentialChoice(set_id)}
    await confirm(service, upload_id, credentials=choice)
    await rig.fresh().actions.deploy(ADMIN, ENV_A, SOURCE, choices=choice)

    [binding] = await binding_rows(rig.db)
    assert (binding["image_reference"], binding["registry_credential_id"]) == (REFERENCE, set_id)
    assert len(host.calls("POST", "/images/create")) == 1


async def test_an_image_whose_digest_differs_on_the_target_is_warned_about(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    from app.services.export_manifest import ImageReference

    rig = stack_rig
    archived = "sha256:" + "ab" * 32
    held = "sha256:" + "cd" * 32
    host.add_image("nginx:1", "sha256:" + "d" * 64, digests=(f"nginx@{held}",))
    service, _ = build_importer(rig)
    archive = await build_archive(
        volumes={}, images=(ImageReference(reference="nginx:1", tag="1", digest=archived),)
    )
    upload_id = await uploaded(service, archive)

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    [image] = review.plan.images

    assert (image.status, image.archive_digest, image.local_digest) == ("differs", archived, held)
    await opened(service, upload_id)
    await confirm(service, upload_id)
    stored_row = await (await stored(rig)).get(ENV_A, SOURCE)
    assert stored_row is not None and "nginx:1" in stored_row.compose_file


async def test_the_format_version_one_fixture_imports(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    upload_id = await uploaded(service, FIXTURE.read_bytes())

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    await opened(service, upload_id, FIXTURE_PASSPHRASE)
    imported = await confirm(service, upload_id, credentials={"nginx:1.27": CredentialChoice(None)})

    assert [item.name for item in review.plan.skipped_volumes] == ["shop-data"]
    assert review.format_version == 1 and imported.volumes == ()
    repository = await stored(stack_rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None
    assert (await repository.reveal_secrets(row.id))[SECRET_NAME] == "fixture-db-password"


async def test_no_secret_or_passphrase_reaches_a_row_a_log_or_the_database_in_the_clear(
    stack_rig: Rig, host: StackImportEngine, caplog: pytest.LogCaptureFixture
) -> None:
    rig = stack_rig
    caplog.set_level(logging.DEBUG)
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    with pytest.raises(UnlockFailed):
        await opened(service, upload_id, WRONG_PASSPHRASE)
    await opened(service, upload_id)
    await confirm(service, upload_id)

    forbidden = [SECRET_VALUE, SECRET_FILE_BODY, PASSPHRASE, WRONG_PASSPHRASE]
    dump = await activity_dump(rig)
    texts = [dump, logged_text(caplog)]
    texts.append(
        json.dumps(
            [
                dict(row)
                for row in await rig.db.fetch_limited(
                    "SELECT value, value_ciphertext FROM stack_variable", (), limit=100
                )
            ],
            default=str,
        )
    )
    for text in texts:
        for value in forbidden:
            assert value not in text


async def test_a_busy_stack_name_refuses_the_import_and_creates_nothing(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    holder = LockHolder(UserActor.of(ADMIN), "other", "redeploy")

    async with rig.engine.locks.hold(ObjectKey(ENV_A, "stack", SOURCE), holder):
        with pytest.raises(ObjectBusy):
            await confirm(service, upload_id)

    assert host.volumes == {}
    assert await (await stored_or_none(rig)).get(ENV_A, SOURCE) is None


async def stored_or_none(rig: Rig) -> StackRepository:
    return StackRepository(rig.db, rig.registry.vault, rig.clock)


def _unused(_: Path, __: ValidationFailed) -> None:
    return None


SWARM_NAME = "swarmshop"
SWARM_VOLUME = f"{SWARM_NAME}_data"
SWARM_FILE = """\
services:
  web:
    image: busybox:latest
    deploy:
      replicas: 1
    volumes:
      - type: volume
        source: data
        target: /var/lib/data
volumes:
  data: {}
"""
NAMESPACE_LABEL = "com.docker.stack.namespace"


class SwarmImportDaemon(StackDaemon, StackImportEngine):
    """A manager with the stack scenarios' refusals that also accepts volume data."""


@pytest.fixture
def swarm_daemon(fake_hosts: dict[str, str]) -> Iterator[SwarmImportDaemon]:
    daemon = SwarmImportDaemon()
    daemon.add_image("busybox:latest")
    yield daemon
    daemon.release_gates()


@pytest.fixture
async def swarm_rig(
    access_template: Path,
    tmp_path: Path,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    swarm_daemon: SwarmImportDaemon,
    tls_workdir: Path,
) -> AsyncIterator[Rig]:
    async with build_swarm_rig(
        access_template, tmp_path, runtime, clock, vault, swarm_daemon
    ) as rig:
        yield rig


async def swarm_archive(secrets: bool = False) -> bytes:
    return await build_archive(
        kind="swarm_stack",
        text=SWARM_FILE,
        name=SWARM_NAME,
        secrets=secrets,
        passphrase=None,
        volumes={SWARM_VOLUME: {NOTE_FILE: NOTE}},
        variables=(),
        files=(),
    )


async def test_a_swarm_archive_imports_into_a_swarm_environment_only(
    swarm_rig: Rig, swarm_daemon: SwarmImportDaemon
) -> None:
    rig = swarm_rig
    service, _ = build_importer(rig)
    archive = await swarm_archive()
    upload_id = await uploaded(service, archive, kind="swarm")

    with pytest.raises(StackUploadGone):
        await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "swarm", upload_id)
    imported = await confirm(
        service,
        upload_id,
        name=SWARM_NAME,
        kind="swarm",
        credentials={"busybox:latest": CredentialChoice(None)},
    )

    assert [(item.kind, item.name) for item in review.plan.objects if item.kind == "service"] == [
        ("service", f"{SWARM_NAME}_web")
    ]
    assert imported.kind == "swarm" and imported.volumes == (SWARM_VOLUME,)
    assert swarm_daemon.volume_labels[SWARM_VOLUME] == {NAMESPACE_LABEL: SWARM_NAME}
    assert swarm_daemon.files_of(SWARM_VOLUME)[NOTE_FILE].data == NOTE
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    row = await repository.get(ENV_A, SWARM_NAME)
    assert row is not None and row.kind == "swarm" and row.compose_file == SWARM_FILE
    [event] = await rig.rows("stack_imported")
    assert event["detail"]["stack_kind"] == "swarm" and event["detail"]["volumes_created"] == 1


async def test_an_archive_of_the_other_kind_or_a_container_is_refused_at_upload(
    stack_rig: Rig, host: StackImportEngine, tmp_path: Path
) -> None:
    service, _ = build_importer(stack_rig)
    swarm = await swarm_archive()

    with pytest.raises(ValidationFailed) as other:
        await uploaded(service, swarm, kind="compose")
    spool = stack_rig.data_dir / "stack-import"

    assert "Swarm stack" in other.value.fields["archive"]
    assert not any(spool.iterdir())


async def test_a_swarm_environment_is_not_found_for_a_compose_only_environment(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    service, _ = build_importer(stack_rig)
    archive = await swarm_archive()

    with pytest.raises(ResourceNotFound):
        await service.receive(ADMIN_SESSION, SESSION, ENV_A, "swarm", parts(archive))


async def test_build_contexts_import_as_uploaded_and_admit_the_files_build(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    from app.services.build_context_archive import inspect_context
    from app.services.export_build import ContextInput
    from tests.archive_support import Member, build_tar
    from tests.stack_build_support import BUILD_FILE, CONTEXT, DOCKERFILE

    rig = stack_rig
    host.add_image(REFERENCE)
    archive = inspect_context(build_tar(Member("Dockerfile", DOCKERFILE.encode())))
    service, _ = build_importer(rig)
    upload = await build_archive(
        text=BUILD_FILE,
        volumes={},
        contexts=(
            ContextInput(
                CONTEXT,
                archive.content,
                archive.compressed,
                archive.member_count,
                archive.extracted_size,
            ),
        ),
    )
    upload_id = await uploaded(service, upload)
    await opened(service, upload_id)

    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    imported = await confirm(service, upload_id)

    assert review.plan.contexts == ((CONTEXT, len(archive.content)),) and imported.contexts == 1
    repository = await stored(rig)
    row = await repository.get(ENV_A, SOURCE)
    assert row is not None
    [context] = await repository.contexts.contexts(row.id)
    stored_archive = await repository.contexts.archive(row.id, CONTEXT)
    assert context.name == CONTEXT and stored_archive is not None
    assert stored_archive.content == archive.content
    assert stored_archive.content_digest == archive.content_digest


async def test_a_request_cancelled_while_a_volume_is_written_removes_it_and_records_it(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    review = await service.review(ADMIN_SESSION, SESSION, ENV_A, "compose", upload_id)
    gate = host.hold("put_archive")
    task = asyncio.ensure_future(
        service.import_stack(
            ADMIN_SESSION,
            ENV_A,
            "compose",
            upload_id,
            ImportChoices(name=SOURCE),
            review.plan.digest,
        )
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.helpers() == [] and host.volumes == {}
    assert rig.engine.locks.holder_of(ObjectKey(ENV_A, "volume", DATA_VOLUME)) is None
    assert rig.engine.locks.holder_of(ObjectKey(ENV_A, "stack", SOURCE)) is None
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    assert await repository.get(ENV_A, SOURCE) is None
    [row] = await rig_rows(rig, "stack_imported", "failure")
    assert row["detail"]["reason"] == "interrupted"  # type: ignore[index]
    assert row["detail"]["volumes_removed"] == DATA_VOLUME  # type: ignore[index]


async def test_a_failure_storing_the_stack_after_the_volumes_were_written_removes_them(
    stack_rig: Rig, host: StackImportEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig

    async def refuse(self: StackService, *args: object, **kwargs: object) -> None:
        raise RuntimeError("the store failed")

    monkeypatch.setattr(StackService, "_store_new", refuse)
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)

    with pytest.raises(StackImportFailed) as failed:
        await confirm(service, upload_id)

    assert failed.value.removed == (DATA_VOLUME,) and host.volumes == {}
    assert not rig.engine.imported_volumes.registered(ENV_A, SOURCE)
    [row] = await rig_rows(rig, "stack_imported", "failure")
    assert row["detail"]["volumes_removed"] == DATA_VOLUME  # type: ignore[index]
    assert await StackRepository(rig.db, rig.registry.vault, rig.clock).get(ENV_A, SOURCE) is None


async def test_a_volume_that_cannot_be_removed_is_named_as_kept(
    stack_rig: Rig, host: StackImportEngine
) -> None:
    rig = stack_rig
    service, _ = build_importer(rig)
    upload_id = await uploaded(service)
    await opened(service, upload_id)
    host.fail_after_members = 0
    host.fail("volume_remove", 500)

    with pytest.raises(StackImportFailed) as failed:
        await confirm(service, upload_id)

    assert failed.value.kept == (DATA_VOLUME,) and DATA_VOLUME in str(failed.value)
    [row] = await rig_rows(rig, "stack_imported", "failure")
    assert row["detail"]["volumes_kept"] == DATA_VOLUME  # type: ignore[index]
