"""Environment creation, editing, tagging, grants and deletion, with the activity each leaves."""

import os
import time
from collections.abc import Awaitable, Callable

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.environment import EnvironmentRepository, StatusRecord
from app.db.repositories.environment_snapshot import (
    EnvironmentSnapshotRepository,
    ObservedTimes,
    SnapshotCounts,
    observed_times,
)
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.docker_engine.probe import ProbeStatus, Stage
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import PLAIN_TCP_REFUSAL, TcpSettings, UnixSocketSettings
from app.netpolicy import REFUSAL_TEXT
from app.services.activity import ActivityRecorder, UserActor
from app.services.assembly import AccessServices, EnvironmentServices, build_environment_services
from app.services.environment_connector import ConnectionTestFailed
from app.services.environments import (
    CHANGED_SINCE_REVIEW,
    MAX_BATCH,
    ConnectionInput,
    EnvironmentBusy,
    EnvironmentSpec,
    TlsInput,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, ENV_B_NAME, STAMP, STANDARD, team_of
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.docker_engine.tls_material import Material, valid_material
from tests.environment_support import (
    LOCAL_ENDPOINT,
    LOCAL_PATH,
    NEW_NAME,
    REMOTE_ENDPOINT,
    REMOTE_HOST,
    RecordingPoker,
    ScriptedProbes,
    actions_recorded,
    activity_rows,
    failed_result,
    local_spec,
    remote_spec,
)
from tests.stack_support import Rig, seed_stack_row

OTHER_PATH = "/run/other-docker.sock"
OTHER_ENDPOINT = f"unix://{OTHER_PATH}"
OTHER_HOST = "other.example.test"
MOVED_PATH = "/run/moved-docker.sock"
FIRST_SEEDED_ID = 1
ABSENT_ID = 9999
OLD_DIRECTORY_AGE = 3600
HELD_TIME = "2099-01-01T00:00:00Z"


@pytest.fixture
def material() -> Material:
    return valid_material()


@pytest.fixture
async def remote(environments: EnvironmentServices, material: Material) -> int:
    """A saved remote environment with TLS material; returns its id."""
    created = await environments.environments.create(ADMIN, remote_spec(material, name="remote"))
    return created.summary.id


@pytest.fixture
async def local(environments: EnvironmentServices) -> int:
    """A saved local environment; returns its id."""
    created = await environments.environments.create(ADMIN, local_spec(name="local"))
    return created.summary.id


@pytest.fixture
def repository(db: Session, clock: FakeClock) -> EnvironmentRepository:
    return EnvironmentRepository(db, clock)


@pytest.fixture
def tls_rows(db: Session, clock: FakeClock, vault: Vault) -> EnvironmentTlsRepository:
    return EnvironmentTlsRepository(db, vault, clock)


async def test_creating_a_local_environment_records_what_the_test_found_and_one_event(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    poker: RecordingPoker,
    db: Session,
) -> None:
    created = await environments.environments.create(ADMIN, local_spec())

    summary = created.summary
    assert (summary.name, summary.is_local, summary.status) == (NEW_NAME, True, "reachable")
    assert (summary.type, summary.engine_version) == ("standalone", "29.2.1")
    assert probes.calls[0].fingerprint is None
    assert isinstance(probes.calls[0].settings, UnixSocketSettings)
    assert poker.poked == [summary.id]
    [row] = [r for r in await activity_rows(db) if r["action"] == "environment_created"]
    assert (row["result"], row["actor_user_id"], row["target_type"]) == (
        "success",
        ADMIN_ID,
        "environment",
    )
    assert (row["target_id"], row["environment_id"]) == (str(summary.id), None)
    assert row["detail"]["endpoint"] == LOCAL_ENDPOINT


async def test_creating_a_remote_environment_stores_the_key_as_ciphertext_and_no_pem_in_events(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    material: Material,
    tls_rows: EnvironmentTlsRepository,
    db: Session,
) -> None:
    created = await environments.environments.create(ADMIN, remote_spec(material))

    stored = await tls_rows.get(created.summary.id)
    assert stored is not None
    assert stored.client_key_ciphertext is not None
    assert material.key.splitlines()[1] not in stored.client_key_ciphertext
    assert await tls_rows.reveal_client_key(created.summary.id) == material.key
    assert isinstance(probes.calls[0].settings, TcpSettings)
    assert created.summary.connection is not None
    assert created.summary.connection.tls is not None
    assert created.summary.connection.tls.leaf_subject == "client"
    events = " ".join(str(row["detail"]) for row in await activity_rows(db))
    assert not any(line in events for line in material.key.splitlines()[1:-1])


async def test_a_remote_environment_without_tls_material_is_refused_with_the_plain_tcp_explanation(
    environments: EnvironmentServices, probes: ScriptedProbes
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, remote_spec(None))

    assert refused.value.fields == {"tls": PLAIN_TCP_REFUSAL}
    assert probes.calls == []


async def test_a_remote_environment_with_only_some_tls_items_is_refused(
    environments: EnvironmentServices, material: Material
) -> None:
    partial = EnvironmentSpec(
        name=NEW_NAME,
        connection=ConnectionInput(kind="tcp_tls", host=REMOTE_HOST),
        tls=TlsInput(ca_certificate=material.ca),
        reviewed_endpoint=REMOTE_ENDPOINT,
    )

    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, partial)

    assert list(refused.value.fields) == ["tls"]


async def test_tls_material_for_a_local_environment_is_refused(
    environments: EnvironmentServices, material: Material
) -> None:
    spec = local_spec(tls=TlsInput(material.ca, material.cert, material.key))

    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, spec)

    assert list(refused.value.fields) == ["tls"]


async def test_a_unix_address_in_the_host_field_is_refused_rather_than_read_as_the_local_socket(
    environments: EnvironmentServices, material: Material
) -> None:
    spec = remote_spec(material, host="unix://host/var/run/docker.sock")

    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, spec)

    assert list(refused.value.fields) == ["host"]


@pytest.mark.parametrize("reviewed", [None, OTHER_ENDPOINT, "unix:///var/run/docker.sock/"])
async def test_an_endpoint_other_than_the_reviewed_one_is_refused(
    environments: EnvironmentServices, probes: ScriptedProbes, reviewed: str | None
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, local_spec(reviewed_endpoint=reviewed))

    assert list(refused.value.fields) == ["reviewed_endpoint"]
    assert probes.calls == []


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "line\nbreak", "zero​width", "bidi‮flip"])
async def test_a_name_that_is_empty_too_long_or_hides_characters_is_refused(
    environments: EnvironmentServices, name: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, local_spec(name=name))

    assert list(refused.value.fields) == ["name"]


async def test_a_second_environment_with_the_same_name_in_any_case_or_endpoint_is_refused(
    environments: EnvironmentServices, local: int
) -> None:
    with pytest.raises(ValidationFailed) as same_name:
        await environments.environments.create(
            ADMIN,
            local_spec(
                name="LOCAL",
                connection=ConnectionInput("unix_socket", OTHER_PATH),
                reviewed_endpoint=OTHER_ENDPOINT,
            ),
        )
    with pytest.raises(ValidationFailed) as same_endpoint:
        await environments.environments.create(ADMIN, local_spec(name="another"))

    assert list(same_name.value.fields) == ["name"]
    assert list(same_endpoint.value.fields) == ["socket_path"]
    assert "local" in same_endpoint.value.fields["socket_path"]


async def test_a_failed_connection_test_refuses_the_save_and_names_the_stage(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    repository: EnvironmentRepository,
    db: Session,
) -> None:
    probes.result = failed_result(Stage.TCP)

    with pytest.raises(ConnectionTestFailed) as refused:
        await environments.environments.create(ADMIN, local_spec())

    assert refused.value.report.failed_stage == "tcp"
    assert await repository.get_by_name(NEW_NAME) is None
    assert await actions_recorded(db) == [("environment_created", "failure")]


async def test_a_failed_connection_test_is_saved_with_its_stage_when_accepted(
    environments: EnvironmentServices, probes: ScriptedProbes
) -> None:
    probes.result = failed_result(Stage.SOCKET, "Nothing exists at this path.")

    created = await environments.environments.create(ADMIN, local_spec(save_anyway=True))

    summary = created.summary
    assert (summary.status, summary.failed_stage) == ("unreachable", "socket")
    assert summary.status_message == "Nothing exists at this path."
    assert summary.last_contacted_at is None


async def test_an_unsupported_engine_version_is_saved_as_version_unsupported(
    environments: EnvironmentServices, probes: ScriptedProbes
) -> None:
    probes.result = failed_result(Stage.ENGINE, "Upgrade Docker.", ProbeStatus.VERSION_UNSUPPORTED)

    created = await environments.environments.create(ADMIN, local_spec(save_anyway=True))

    assert (created.summary.status, created.summary.failed_stage) == (
        "version_unsupported",
        "engine",
    )


async def test_only_an_administrator_creates_an_environment_and_the_refusal_leaves_no_event(
    environments: EnvironmentServices, db: Session
) -> None:
    with pytest.raises(PermissionDenied):
        await environments.environments.create(STANDARD, local_spec())

    assert await actions_recorded(db) == []


async def test_a_rename_never_reaches_the_host_and_records_the_previous_name(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    poker: RecordingPoker,
    local: int,
    db: Session,
) -> None:
    probes.calls.clear()
    poker.poked.clear()

    updated = await environments.environments.update(
        ADMIN, local, local_spec(name="renamed", reviewed_endpoint=LOCAL_ENDPOINT)
    )

    assert updated.summary.name == "renamed"
    assert probes.calls == []
    assert poker.poked == []
    row = (await activity_rows(db))[-1]
    assert (row["action"], row["result"], row["target_id"]) == (
        "environment_updated",
        "success",
        str(local),
    )
    assert row["detail"]["previous_name"] == "local"


async def test_saving_an_edit_that_changes_nothing_writes_no_event(
    environments: EnvironmentServices, local: int, db: Session
) -> None:
    before = len(await activity_rows(db))

    await environments.environments.update(ADMIN, local, local_spec(name="local"))

    assert len(await activity_rows(db)) == before


async def test_changing_the_endpoint_retests_clears_the_snapshot_and_drops_the_client(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    poker: RecordingPoker,
    client_registry: EngineClientRegistry,
    clients_built: list[object],
    local: int,
    db: Session,
) -> None:
    await EnvironmentSnapshotRepository(db).record_counts(
        local, "2026-01-01T00:00:00Z", SnapshotCounts(container_count=3)
    )
    client = await client_registry.client_for(local, "fingerprint", lambda: UnixSocketSettings())
    probes.calls.clear()
    poker.poked.clear()
    spec = local_spec(
        name="local",
        connection=ConnectionInput("unix_socket", OTHER_PATH),
        reviewed_endpoint=OTHER_ENDPOINT,
    )

    updated = await environments.environments.update(ADMIN, local, spec)

    assert updated.summary.connection is not None
    assert updated.summary.connection.endpoint == OTHER_ENDPOINT
    assert len(probes.calls) == 1
    assert poker.poked == [local]
    assert await EnvironmentSnapshotRepository(db).get(local) is None
    assert client.closed
    row = (await activity_rows(db))[-1]
    assert (row["detail"]["previous_endpoint"], row["detail"]["endpoint"]) == (
        LOCAL_ENDPOINT,
        OTHER_ENDPOINT,
    )


async def test_changing_the_endpoint_drops_the_check_times_held_in_memory(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    local: int,
    db: Session,
) -> None:
    observed_times(db).hold(
        local,
        ObservedTimes(checked_at=HELD_TIME, contacted_at=HELD_TIME, captured_at=HELD_TIME),
    )
    spec = local_spec(
        name="local",
        connection=ConnectionInput("unix_socket", OTHER_PATH),
        reviewed_endpoint=OTHER_ENDPOINT,
    )

    await environments.environments.update(ADMIN, local, spec)

    changed = await repository.get(local)
    assert changed is not None
    assert HELD_TIME not in (changed.last_checked_at, changed.last_contacted_at)


async def test_renaming_an_environment_keeps_the_check_times_held_in_memory(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    local: int,
    db: Session,
) -> None:
    observed_times(db).hold(
        local,
        ObservedTimes(checked_at=HELD_TIME, contacted_at=HELD_TIME, captured_at=HELD_TIME),
    )

    await environments.environments.update(
        ADMIN, local, local_spec(name="renamed", reviewed_endpoint=LOCAL_ENDPOINT)
    )

    renamed = await repository.get(local)
    assert renamed is not None
    assert (renamed.last_checked_at, renamed.last_contacted_at) == (HELD_TIME, HELD_TIME)


async def test_replacing_the_tls_material_stores_a_new_key_and_retests(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    remote: int,
    tls_rows: EnvironmentTlsRepository,
) -> None:
    before = await tls_rows.get(remote)
    replacement = valid_material("ed25519")
    probes.calls.clear()

    await environments.environments.update(ADMIN, remote, remote_spec(replacement, name="remote"))

    after = await tls_rows.get(remote)
    assert before is not None and after is not None
    assert after.client_certificate != before.client_certificate
    assert await tls_rows.reveal_client_key(remote) == replacement.key
    assert len(probes.calls) == 1


async def test_an_edit_with_empty_tls_inputs_keeps_the_stored_material_and_retests_a_new_host(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    remote: int,
    tls_rows: EnvironmentTlsRepository,
    material: Material,
) -> None:
    probes.calls.clear()

    await environments.environments.update(
        ADMIN, remote, remote_spec(None, name="remote", host=OTHER_HOST)
    )

    assert await tls_rows.reveal_client_key(remote) == material.key
    assert isinstance(probes.calls[0].settings, TcpSettings)
    assert probes.calls[0].settings.host == OTHER_HOST


async def test_switching_a_remote_environment_to_the_local_socket_deletes_its_tls_material(
    environments: EnvironmentServices, remote: int, tls_rows: EnvironmentTlsRepository
) -> None:
    await environments.environments.update(ADMIN, remote, local_spec(name="remote"))

    assert await tls_rows.get(remote) is None


async def test_a_connection_test_failure_during_an_edit_leaves_the_environment_unchanged(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    local: int,
    repository: EnvironmentRepository,
) -> None:
    probes.result = failed_result(Stage.SOCKET)
    spec = local_spec(
        name="local",
        connection=ConnectionInput("unix_socket", OTHER_PATH),
        reviewed_endpoint=OTHER_ENDPOINT,
    )

    with pytest.raises(ConnectionTestFailed):
        await environments.environments.update(ADMIN, local, spec)

    current = await repository.get(local)
    assert current is not None
    assert current.endpoint == LOCAL_PATH


async def test_an_explicit_test_stores_the_status_and_is_recorded_with_the_stage(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    local: int,
    repository: EnvironmentRepository,
    db: Session,
) -> None:
    probes.result = failed_result(Stage.SOCKET, "Permission denied.")

    report = await environments.environments.test_connection(ADMIN, local)

    stored = await repository.get(local)
    assert stored is not None
    assert (report.failed_stage, stored.status, stored.failed_stage) == (
        "socket",
        "unreachable",
        "socket",
    )
    row = (await activity_rows(db))[-1]
    assert row["action"] == "environment_connection_tested"
    assert (row["detail"]["status"], row["detail"]["failed_stage"]) == ("unreachable", "socket")


async def test_tags_are_set_as_one_event_naming_what_was_added_and_removed(
    environments: EnvironmentServices, local: int, db: Session
) -> None:
    first = await environments.tags.create(ADMIN, name="blue")
    second = await environments.tags.create(ADMIN, name="green")
    await environments.environments.set_tags(ADMIN, local, [first.id])

    tags = await environments.environments.set_tags(ADMIN, local, [second.id])

    assert [tag.name for tag in tags] == ["green"]
    row = (await activity_rows(db))[-1]
    assert (row["action"], row["detail"]["added"], row["detail"]["removed"]) == (
        "environment_tags_set",
        "green",
        "blue",
    )


async def test_setting_the_tags_an_environment_already_has_writes_no_event(
    environments: EnvironmentServices, local: int, db: Session
) -> None:
    tag = await environments.tags.create(ADMIN, name="blue")
    await environments.environments.set_tags(ADMIN, local, [tag.id])
    before = len(await activity_rows(db))

    await environments.environments.set_tags(ADMIN, local, [tag.id])

    assert len(await activity_rows(db)) == before


async def test_a_tag_that_no_longer_exists_is_refused(
    environments: EnvironmentServices, local: int
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.set_tags(ADMIN, local, [ABSENT_ID])

    assert list(refused.value.fields) == ["tags"]


async def test_assigning_a_group_records_the_group_and_the_one_it_left(
    environments: EnvironmentServices, local: int, db: Session
) -> None:
    first = await environments.groups.create(ADMIN, name="first")
    second = await environments.groups.create(ADMIN, name="second")
    await environments.environments.assign_group(ADMIN, local, first.id)

    await environments.environments.assign_group(ADMIN, local, second.id)

    row = (await activity_rows(db))[-1]
    assert (row["action"], row["detail"]["group"], row["detail"]["previous_group"]) == (
        "environment_group_assigned",
        "second",
        "first",
    )


async def test_granting_access_names_the_grantee_and_a_repeat_is_refused(
    environments: EnvironmentServices, access: AccessServices, local: int, db: Session
) -> None:
    team_id = await team_of(access, "platform")
    await environments.environments.grant(ADMIN, local, user_id=STANDARD_ID)
    await environments.environments.grant(ADMIN, local, team_id=team_id)

    with pytest.raises(ValidationFailed, match="already exists"):
        await environments.environments.grant(ADMIN, local, user_id=STANDARD_ID)

    granted = [r for r in await activity_rows(db) if r["action"] == "environment_access_granted"]
    successes = [r for r in granted if r["result"] == "success"]
    assert [(r["detail"]["grantee_type"], r["detail"]["grantee"]) for r in successes] == [
        ("user", "standard-user"),
        ("team", "platform"),
    ]


async def test_revoking_a_grant_that_was_never_made_is_refused_and_a_real_one_is_recorded(
    environments: EnvironmentServices, local: int, db: Session
) -> None:
    await environments.environments.grant(ADMIN, local, user_id=STANDARD_ID)

    await environments.environments.revoke(ADMIN, local, user_id=STANDARD_ID)
    with pytest.raises(ValidationFailed, match="does not exist"):
        await environments.environments.revoke(ADMIN, local, user_id=STANDARD_ID)

    assert [a for a in await actions_recorded(db) if a[0] == "environment_access_revoked"] == [
        ("environment_access_revoked", "success"),
        ("environment_access_revoked", "failure"),
    ]


async def test_naming_both_a_user_and_a_team_in_a_grant_is_refused(
    environments: EnvironmentServices, local: int
) -> None:
    with pytest.raises(ValidationFailed, match="one user or one team"):
        await environments.environments.grant(ADMIN, local, user_id=STANDARD_ID, team_id=1)


async def test_deleting_an_environment_removes_it_detaches_history_and_drops_its_client(
    environments: EnvironmentServices,
    client_registry: EngineClientRegistry,
    repository: EnvironmentRepository,
    local: int,
    db: Session,
    clock: FakeClock,
) -> None:
    client = await client_registry.client_for(local, "fingerprint", lambda: UnixSocketSettings())
    await ActivityRecorder(db, clock).record_event(
        UserActor(ADMIN_ID),
        action="environment_connection_tested",
        target_type="environment",
        target_id=str(local),
        environment_id=local,
        result="success",
    )

    await environments.environments.delete(ADMIN, local, expected_name="local")

    assert await repository.get(local) is None
    assert client.closed
    rows = await activity_rows(db)
    assert {row["environment_id"] for row in rows} == {None}
    deleted = rows[-1]
    assert (deleted["action"], deleted["result"], deleted["target_id"]) == (
        "environment_deleted",
        "success",
        str(local),
    )
    assert deleted["detail"] == {
        "name": "local",
        "kind": "unix_socket",
        "endpoint": LOCAL_ENDPOINT,
        "rules": 0,
        "job_runs": 0,
    }


async def test_a_deleted_environments_id_is_not_given_to_the_next_one(
    environments: EnvironmentServices, local: int
) -> None:
    await environments.environments.delete(ADMIN, local, expected_name="local")

    created = await environments.environments.create(ADMIN, local_spec(name="successor"))

    assert created.summary.id > local


async def test_a_stale_confirmation_deletes_nothing_and_reports_the_change(
    environments: EnvironmentServices, repository: EnvironmentRepository, local: int
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.delete(ADMIN, local, expected_name="something else")

    assert refused.value.fields == {"name": CHANGED_SINCE_REVIEW}
    assert await repository.get(local) is not None


async def test_a_batch_delete_reports_each_environment_on_its_own(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    probes: ScriptedProbes,
    local: int,
    db: Session,
) -> None:
    second = (
        await environments.environments.create(
            ADMIN,
            local_spec(
                name="second",
                connection=ConnectionInput("unix_socket", OTHER_PATH),
                reviewed_endpoint=OTHER_ENDPOINT,
            ),
        )
    ).summary.id

    result = await environments.environments.delete_many(
        ADMIN,
        [local, ABSENT_ID, second],
        expected_names={local: "local", ABSENT_ID: "ghost", second: "renamed since review"},
    )

    assert [(item.environment_id, item.outcome) for item in result.items] == [
        (local, "deleted"),
        (ABSENT_ID, "not_found"),
        (second, "changed"),
    ]
    assert result.deleted == 1
    assert await repository.get(second) is not None
    assert ("environment_deleted", "success") in await actions_recorded(db)


async def test_a_batch_of_more_than_fifty_environments_is_refused_before_anything_is_deleted(
    environments: EnvironmentServices, repository: EnvironmentRepository, local: int
) -> None:
    ids = [local, *range(1000, 1000 + MAX_BATCH)]

    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.delete_many(
            ADMIN, ids, expected_names=dict.fromkeys(ids, "local")
        )

    assert list(refused.value.fields) == ["environments"]
    assert await repository.get(local) is not None


async def test_a_batch_of_exactly_fifty_environments_is_accepted(
    environments: EnvironmentServices,
) -> None:
    ids = list(range(1000, 1000 + MAX_BATCH))

    result = await environments.environments.delete_many(ADMIN, ids, expected_names={})

    assert len(result.items) == MAX_BATCH


async def test_an_empty_batch_is_refused(environments: EnvironmentServices) -> None:
    with pytest.raises(ValidationFailed):
        await environments.environments.delete_many(ADMIN, [], expected_names={})


async def test_the_deletion_preview_names_who_loses_access_and_what_goes_with_the_environment(
    environments: EnvironmentServices,
    access: AccessServices,
    local: int,
    db: Session,
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    await environments.environments.assign_group(ADMIN, local, group.id)
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)
    team_id = await team_of(access, "platform")
    await environments.environments.grant(ADMIN, local, team_id=team_id)
    tag = await environments.tags.create(ADMIN, name="blue")
    await environments.environments.set_tags(ADMIN, local, [tag.id])
    await EnvironmentSnapshotRepository(db).record_counts(
        local, "2026-01-01T00:00:00Z", SnapshotCounts(container_count=1)
    )

    preview = await environments.environments.delete_preview(ADMIN, [local, ABSENT_ID])

    [item] = preview.items
    assert preview.missing == (ABSENT_ID,)
    assert (item.name, item.is_local, item.endpoint) == ("local", True, LOCAL_ENDPOINT)
    assert [team.name for team in item.direct.teams] == ["platform"]
    assert item.group is not None
    assert [user.name for user in item.group.grants.users] == ["standard-user"]
    assert item.standard_users_affected == 1
    counts = {record.label: record.count for record in item.records}
    assert (counts["Tag links"], counts["Access grants"], counts["Snapshot"]) == (1, 1, 1)
    assert {record.label: record.count for record in preview.totals} == counts
    assert "keep running" in item.host_note
    assert "kept" in item.activity_note


async def test_the_deletion_preview_changes_nothing(
    environments: EnvironmentServices, repository: EnvironmentRepository, local: int, db: Session
) -> None:
    before = await actions_recorded(db)

    await environments.environments.delete_preview(ADMIN, [local])

    assert await repository.get(local) is not None
    assert await actions_recorded(db) == before


@pytest.mark.parametrize(
    "operation",
    [
        lambda services: services.environments.delete_preview(STANDARD, [ENV_A]),
        lambda services: services.environments.delete_many(STANDARD, [ENV_A], expected_names={}),
        lambda services: services.environments.preview(STANDARD, ConnectionInput("unix_socket")),
    ],
    ids=["delete_preview", "delete_many", "preview"],
)
async def test_a_standard_user_cannot_use_the_administrator_only_environment_operations(
    environments: EnvironmentServices, operation: Callable[[EnvironmentServices], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionDenied):
        await operation(environments)


async def test_a_missing_environment_is_not_found_for_an_administrator(
    environments: EnvironmentServices,
) -> None:
    with pytest.raises(ResourceNotFound):
        await environments.environments.get(ADMIN, ABSENT_ID)


async def test_the_preview_of_a_changed_endpoint_says_so_and_flags_discarded_tls_material(
    environments: EnvironmentServices, remote: int
) -> None:
    preview = await environments.environments.preview(
        ADMIN, ConnectionInput("unix_socket", LOCAL_PATH), environment_id=remote
    )

    assert preview.endpoint == LOCAL_ENDPOINT
    assert (preview.changes_endpoint, preview.discards_tls_material) == (True, True)
    assert "host this product runs on" in preview.note


@pytest.fixture
def repointing(
    probes: ScriptedProbes, repository: EnvironmentRepository, local: int
) -> ScriptedProbes:
    """Probes that repoint the saved environment at another socket while a test is running."""

    async def repoint() -> None:
        await repository.replace_connection(
            local,
            connection_kind="unix_socket",
            socket_path=MOVED_PATH,
            url=None,
            record=StatusRecord("unknown", None, None, STAMP),
        )

    probes.before = repoint
    return probes


async def test_an_edit_whose_target_was_repointed_during_its_test_is_refused_and_stores_nothing(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    repointing: ScriptedProbes,
    local: int,
) -> None:
    spec = local_spec(
        name="local",
        connection=ConnectionInput("unix_socket", OTHER_PATH),
        reviewed_endpoint=OTHER_ENDPOINT,
    )

    with pytest.raises(ValidationFailed, match="changed by someone else"):
        await environments.environments.update(ADMIN, local, spec)

    current = await repository.get(local)
    assert current is not None and current.endpoint == MOVED_PATH


async def test_a_connection_test_of_a_host_repointed_meanwhile_does_not_store_its_status(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    repointing: ScriptedProbes,
    local: int,
    db: Session,
) -> None:
    await environments.environments.test_connection(ADMIN, local)

    current = await repository.get(local)
    row = (await activity_rows(db))[-1]
    assert current is not None and current.status == "unknown"
    assert row["detail"]["recorded"] is False


@pytest.mark.parametrize(
    ("group_id", "tag_ids", "field"),
    [
        (ABSENT_ID, [], "group"),
        (0, [], "group"),
        (2**63, [], "group"),
        (None, [ABSENT_ID], "tags"),
        (None, [0], "tags"),
        (None, list(range(1, 22)), "tags"),
    ],
    ids=["absent group", "group zero", "group beyond int64", "absent tag", "tag zero", "21 tags"],
)
async def test_a_group_or_tag_selection_that_could_not_be_applied_is_refused_up_front(
    environments: EnvironmentServices,
    local: int,
    group_id: int | None,
    tag_ids: list[int],
    field: str,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.check_assignment(
            ADMIN, group_id=group_id, tag_ids=tag_ids, environment_id=local
        )

    assert list(refused.value.fields) == [field]


async def test_checking_an_assignment_for_an_environment_the_caller_cannot_see_is_not_found(
    environments: EnvironmentServices,
) -> None:
    with pytest.raises(ResourceNotFound):
        await environments.environments.check_assignment(
            STANDARD, group_id=None, tag_ids=[], environment_id=ABSENT_ID
        )


async def test_a_user_reaching_an_environment_three_ways_is_counted_once_in_its_deletion_impact(
    environments: EnvironmentServices, access: AccessServices, local: int
) -> None:
    team_id = await team_of(access, "platform", STANDARD)
    group = await environments.groups.create(ADMIN, name="production")
    await environments.environments.assign_group(ADMIN, local, group.id)
    await environments.environments.grant(ADMIN, local, user_id=STANDARD_ID)
    await environments.environments.grant(ADMIN, local, team_id=team_id)
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)

    preview = await environments.environments.delete_preview(ADMIN, [local])

    assert preview.items[0].standard_users_affected == 1


async def test_candidates_for_a_group_exclude_its_members_and_name_the_group_of_the_rest(
    environments: EnvironmentServices, local: int
) -> None:
    first = await environments.groups.create(ADMIN, name="first")
    second = await environments.groups.create(ADMIN, name="second")
    await environments.groups.assign_environments(ADMIN, first.id, [ENV_A])
    await environments.groups.assign_environments(ADMIN, second.id, [ENV_B])

    candidates = await environments.groups.candidate_environments(ADMIN, first.id, q=None)

    shown = {c.name: (c.group.name if c.group else None) for c in candidates}
    assert shown == {"environment-b": "second", "local": None}
    assert (await environments.groups.candidate_environments(ADMIN, first.id, q="LOC"))[
        0
    ].name == "local"


async def test_deleting_an_environment_removes_its_stacks_and_sweeps_their_old_directories(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    old = await seed_stack_row(rig, ENV_B, "oldstack", "services: {}")
    young = await seed_stack_row(rig, ENV_B, "youngstack", "services: {}")
    base = rig.compose.files.base
    for token in (old.token, young.token):
        (base / token).mkdir()
    aged = time.time() - OLD_DIRECTORY_AGE
    os.utime(base / old.token, (aged, aged))
    service = build_environment_services(
        rig.db, rig.runtime, rig.engine, rig.access.authoriser, ActivityRecorder(rig.db, rig.clock)
    ).environments

    await service.delete(ADMIN, ENV_B, expected_name=ENV_B_NAME)

    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    assert [path.name for path in base.iterdir()] == [young.token]


async def automation_job(db: Session, clock: FakeClock, environment_id: int, name: str) -> int:
    """Insert an enabled rule and its job in the environment and return the job's id."""
    rule = await AutomationRuleRepository(db, clock).insert(
        name=name,
        subsystem="stack-restarter",
        environment_id=environment_id,
        target_selector="",
        parameters="{}",
        is_enabled=True,
        created_by_user_id=ADMIN_ID,
    )
    job = await ScheduledJobRepository(db, clock).insert(
        subsystem="stack-restarter",
        rule_id=rule.id,
        environment_id=environment_id,
        schedule_kind="interval",
        schedule_expression="3600",
        time_zone="UTC",
        overrun_policy="skip",
        catch_up=False,
        failure_threshold=5,
        next_run_at=None,
    )
    return job.id


async def finished_runs(db: Session, clock: FakeClock, job_id: int, count: int) -> None:
    runs = JobRunRepository(db, clock)
    for _ in range(count):
        started = await runs.start(job_id, trigger="schedule", scheduled_for=None)
        await runs.finish(started.id, outcome="success")


async def test_the_delete_impact_counts_rules_jobs_and_runs_of_that_environment_only(
    environments: EnvironmentServices, db: Session, clock: FakeClock
) -> None:
    mine = await automation_job(db, clock, ENV_A, "mine")
    theirs = await automation_job(db, clock, ENV_B, "theirs")
    await finished_runs(db, clock, mine, 3)
    await finished_runs(db, clock, theirs, 2)

    preview = await environments.environments.delete_preview(ADMIN, [ENV_A])

    counts = {record.label: record.count for record in preview.items[0].records}
    assert (counts["Automation rules"], counts["Scheduled jobs"], counts["Job runs"]) == (1, 1, 3)


async def test_an_environment_is_not_deleted_while_one_of_its_jobs_has_a_run_in_flight(
    environments: EnvironmentServices, db: Session, clock: FakeClock
) -> None:
    name = (await environments.environments.delete_preview(ADMIN, [ENV_A])).items[0].name
    job = await automation_job(db, clock, ENV_A, "mine")
    await finished_runs(db, clock, job, 3)
    elsewhere = await automation_job(db, clock, ENV_B, "theirs")
    await JobRunRepository(db, clock).start(elsewhere, trigger="manual", scheduled_for=None)
    running = await JobRunRepository(db, clock).start(job, trigger="manual", scheduled_for=None)

    with pytest.raises(EnvironmentBusy) as busy:
        await environments.environments.delete(ADMIN, ENV_A, expected_name=name)
    batch = await environments.environments.delete_many(
        ADMIN, [ENV_A], expected_names={ENV_A: name}
    )
    await JobRunRepository(db, clock).finish(running.id, outcome="success")
    await environments.environments.delete(ADMIN, ENV_A, expected_name=name)

    assert [(item.outcome, item.message) for item in batch.items] == [("failed", str(busy.value))]
    rows = await activity_rows(db)
    refusals = [
        r for r in rows if r["action"] == "environment_deleted" and r["result"] == "failure"
    ]
    assert {r["detail"]["reason"] for r in refusals} == {"automation_running"}
    deleted = rows[-1]
    assert (deleted["action"], deleted["result"]) == ("environment_deleted", "success")
    assert (deleted["detail"]["rules"], deleted["detail"]["job_runs"]) == (1, 4)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "169.254.169.254", "0.0.0.0", "[::1]", "metadata.google.internal"],
)
async def test_a_remote_host_on_an_internal_address_is_refused_before_anything_is_probed(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    material: Material,
    db: Session,
    host: str,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.create(ADMIN, remote_spec(material, host=host))

    assert refused.value.fields == {"host": REFUSAL_TEXT}
    assert probes.calls == []
    assert await db.fetch_one("SELECT 1 FROM environment WHERE name = ?", (NEW_NAME,)) is None


async def test_a_permit_lets_an_internal_remote_host_be_saved_and_probed(
    environments: EnvironmentServices,
    probes: ScriptedProbes,
    material: Material,
    address_policy: Callable[..., None],
) -> None:
    address_policy("127.0.0.0/8")

    created = await environments.environments.create(ADMIN, remote_spec(material, host="127.0.0.1"))

    assert created.summary.name == NEW_NAME
    assert len(probes.calls) == 1


async def test_the_local_socket_needs_no_permit(environments: EnvironmentServices) -> None:
    created = await environments.environments.create(ADMIN, local_spec())

    assert created.summary.is_local
