"""Reading a project or namespace from its host, then adopting it as is."""

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.self_identity import HomeEnvironment, SelfIdentity
from app.services.stack_common import changed_since_deploy
from app.services.stack_deploy import ChangedSinceReview
from app.services.stack_read_result import HostReadResult, NeedsFile
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.host_read_support import (
    PROJECT,
    WORKING_DIR,
    ReaderEngine,
    add_project_container,
    compose_labels,
    services_with_identity,
)
from tests.stack_files_support import use_files_compose, use_files_compose_for_swarm
from tests.stack_support import SECRET_SENTINEL, Rig, activity_dump, read_records
from tests.swarm_stack_support import NAME, StackDaemon, create_stack, deploy_stack

HELPER_VERBS = ["create", "remove"]
OWN_CONTAINER = f"{0xD0:064x}"


@pytest.fixture
def host(fake_hosts: dict[str, str]) -> Iterator[ReaderEngine]:
    daemon = ReaderEngine()
    yield daemon
    daemon.release_gates()


def project_files(record: Path) -> str:
    return f"""\
x-fake:
  record: {record}
services:
  web:
    image: nginx:1
    env_file:
      - ./env/app.env
    configs:
      - source: site
        target: /etc/site.conf
    secrets:
      - token
configs:
  site:
    file: ./conf/site.conf
secrets:
  token:
    file: ./secrets/token.txt
"""


async def test_a_project_is_read_audited_and_adopted_as_is_without_touching_the_host(
    stack_rig: Rig, tmp_path: Path
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    await use_files_compose(rig)
    record = tmp_path / "record"
    host.serve("compose.yaml", project_files(record))
    host.serve("env/app.env", f"API_TOKEN={SECRET_SENTINEL}\n")
    host.serve("conf/site.conf", "listen 80;\n")
    host.serve("secrets/token.txt", SECRET_SENTINEL)
    host.serve(".env", f"REGION=eu\nDB_PASSWORD={SECRET_SENTINEL}\n")
    box = add_project_container(
        host, 1, compose_labels(environment_file=f"{WORKING_DIR}/.env"), state="running"
    )
    boxes_before = set(host.boxes)

    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert isinstance(report.outcome, HostReadResult), report.outcome
    result = report.outcome
    assert (result.file_source, report.attempted) == ("read", None)
    assert report.services == ("web",)
    assert {item.name for item in result.files} == {
        "env_app.env",
        "conf_site.conf",
        "secrets_token.txt",
    }
    assert [(v.name, v.secret) for v in result.variables] == [
        ("REGION", False),
        ("DB_PASSWORD", True),
    ]
    assert host.verbs() == HELPER_VERBS and host.helper_boxes() == []
    [read_row] = await rig.rows("stack_files_read")
    assert read_row["result"] == "success"
    assert (read_row["target_type"], read_row["target_id"], read_row["environment_id"]) == (
        "stack",
        PROJECT,
        ENV_B,
    )
    assert read_row["detail"]["file_source"] == "read"
    assert read_row["detail"]["files_read"] == 5
    assert len(read_row["detail"]["helper_container"]) == 64
    assert "refused" not in read_row["detail"]

    adopted = await rig.fresh().actions.adopt(
        ADMIN, ENV_B, name=PROJECT, prepared=result, as_is=True
    )

    stored = await rig.fresh().queries.require_stack(ADMIN, ENV_B, PROJECT, "read")
    detail = await rig.fresh().queries.detail(ADMIN, ENV_B, PROJECT)
    assert adopted.containers == 1
    assert stored.deployed_file_digest == stored.compose_file_digest
    assert stored.deployed_inputs_digest == stored.inputs_digest is not None
    assert stored.resolved_digest and stored.last_deployed_at is None
    assert not changed_since_deploy(
        stored.compose_file_digest,
        stored.deployed_file_digest,
        stored.inputs_digest,
        stored.deployed_inputs_digest,
    )
    assert detail.row.changed_since_deploy is False
    assert host.verbs() == HELPER_VERBS and set(host.boxes) == boxes_before
    assert rig.sim.created == 0 and host.boxes[box.id].state == "running"
    commands = {call["cmd"] for call in read_records(record)}
    assert commands and commands <= {"config"}
    files = {f.name: f.secret for f in await rig.fresh().queries.stored_files(stored)}
    assert files == {"env_app.env": True, "conf_site.conf": False, "secrets_token.txt": True}
    [adopt_row] = await rig.rows("stack_adopted")
    assert adopt_row["result"] == "success"
    assert adopt_row["detail"]["file_source"] == "read" and adopt_row["detail"]["as_is"] is True
    assert SECRET_SENTINEL not in await activity_dump(rig)


async def test_an_override_pair_is_merged_by_the_compose_wrapper_and_marked_merged(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    host.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    host.serve("compose.override.yaml", "services:\n  web:\n    ports:\n      - '8080:80'\n")
    labels = compose_labels(
        config_files=f"{WORKING_DIR}/compose.yaml,{WORKING_DIR}/compose.override.yaml"
    )
    add_project_container(host, 1, labels)

    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert isinstance(report.outcome, HostReadResult), report.outcome
    assert report.outcome.file_source == "merged" and report.attempted is None
    web = yaml.safe_load(report.outcome.compose_text)["services"]["web"]
    assert web == {"image": "nginx:1", "ports": ["8080:80"]}
    assert report.files_read == ("compose.yaml", "compose.override.yaml")
    [read_row] = await rig.rows("stack_files_read")
    assert read_row["detail"]["file_source"] == "merged"


async def test_a_project_whose_files_are_gone_is_generated_from_its_containers_and_adopted(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    add_project_container(
        host, 1, compose_labels(), env=["PATH=/usr/bin", f"API_TOKEN={SECRET_SENTINEL}"]
    )

    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert isinstance(report.outcome, HostReadResult), report.outcome
    assert report.outcome.file_source == "generated"
    assert report.attempted == NeedsFile("missing", "compose.yaml")
    assert report.refused == (("compose.yaml", "missing"),)
    assert SECRET_SENTINEL not in report.outcome.compose_text
    assert [(v.name, v.secret) for v in report.outcome.variables] == [("API_TOKEN", True)]
    assert yaml.safe_load(report.outcome.compose_text)["services"]["web"]["image"] == "nginx:1"
    [read_row] = await rig.rows("stack_files_read")
    assert read_row["detail"]["file_source"] == "generated"
    assert read_row["detail"]["fallback_reason"] == "missing"
    assert read_row["detail"]["refused"] == "missing"

    await rig.fresh().actions.adopt(ADMIN, ENV_B, name=PROJECT, prepared=report.outcome, as_is=True)

    [adopt_row] = await rig.rows("stack_adopted")
    assert adopt_row["detail"]["file_source"] == "generated" and adopt_row["detail"]["as_is"]
    assert rig.host.verbs() == HELPER_VERBS
    assert SECRET_SENTINEL not in await activity_dump(rig)


async def test_a_project_that_neither_files_nor_containers_can_describe_needs_a_file(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    labels = {k: v for k, v in compose_labels().items() if k != "com.docker.compose.service"}
    add_project_container(rig.host, 1, labels)

    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert report.result is None
    assert isinstance(report.outcome, NeedsFile) and report.attempted is not None
    [read_row] = await rig.rows("stack_files_read")
    assert read_row["detail"]["file_source"] == "none"
    assert read_row["detail"]["needs_file"] == report.outcome.reason


async def test_a_standard_user_cannot_read_a_project_or_adopt_what_was_read(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    add_project_container(rig.host, 1, compose_labels())
    generated = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)
    assert isinstance(generated.outcome, HostReadResult)
    helpers_before = rig.host.verbs()

    with pytest.raises((PermissionDenied, ResourceNotFound)):
        await rig.fresh().reader.read_compose(STANDARD, ENV_B, PROJECT)
    with pytest.raises(PermissionDenied):
        await rig.fresh().actions.adopt(
            STANDARD, ENV_B, name=PROJECT, prepared=generated.outcome, as_is=True
        )
    with pytest.raises(ValueError, match="prepared"):
        await rig.fresh().actions.adopt(ADMIN, ENV_B, name=PROJECT, compose_text="x", as_is=True)

    assert rig.host.verbs() == helpers_before
    assert len(await rig.rows("stack_files_read")) == 1


async def test_a_namespace_is_reconstructed_and_adopted_as_is_without_a_service_changing(
    defaulting_stack_rig: Rig, tmp_path: Path
) -> None:
    rig = defaulting_stack_rig
    daemon = rig.host
    assert isinstance(daemon, StackDaemon)
    await use_files_compose_for_swarm(rig, tmp_path / "files-compose")
    await create_stack(rig)
    await deploy_stack(rig)
    deployed = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME)
    await rig.fresh().swarm_actions.forget(ADMIN, ENV_A, NAME, stack_id=deployed.id)
    versions = {i: s["Version"]["Index"] for i, s in daemon.services.items()}
    writes = len(daemon.mutating())

    report = await rig.fresh().reader.read_swarm(ADMIN, ENV_A, NAME)

    assert isinstance(report.outcome, HostReadResult), report.outcome
    assert report.outcome.file_source == "reconstructed"
    assert report.services == ("api", "web")
    [read_row] = await rig.rows("stack_files_read")
    assert read_row["detail"]["file_source"] == "reconstructed"
    assert read_row["detail"]["kind"] == "swarm" and "helper_container" not in read_row["detail"]

    await rig.fresh().swarm_adopter.adopt(ADMIN, ENV_A, NAME, prepared=report.outcome, as_is=True)

    row = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME)
    assert row.deployed_file_digest == row.compose_file_digest
    assert row.deployed_inputs_digest == row.inputs_digest
    assert row.last_deployed_at is None
    assert len(daemon.mutating()) == writes
    assert versions == {i: s["Version"]["Index"] for i, s in daemon.services.items()}
    [adopt_row] = await rig.rows("stack_adopted")
    assert adopt_row["detail"]["file_source"] == "reconstructed" and adopt_row["detail"]["as_is"]


async def test_adopting_as_is_after_the_project_was_redeployed_is_refused_and_stores_nothing(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    host.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    box = add_project_container(host, 1, compose_labels(), state="running")
    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)
    assert isinstance(report.outcome, HostReadResult)
    del host.boxes[box.id]
    add_project_container(host, 2, compose_labels(), state="running")

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.adopt(
            ADMIN, ENV_B, name=PROJECT, prepared=report.outcome, as_is=True
        )

    assert [u.name for u in await rig.fresh().queries.unmanaged(ADMIN, ENV_B)] == [PROJECT]
    [attempt] = await rig.rows("stack_adopted")
    assert (attempt["result"], attempt["detail"]["reason"]) == ("failure", "changed_since_review")


async def test_a_one_off_container_started_after_the_read_does_not_block_adopting(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    host.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    add_project_container(host, 1, compose_labels(), state="running")
    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)
    assert isinstance(report.outcome, HostReadResult)
    add_project_container(
        host, 2, {**compose_labels(), "com.docker.compose.oneoff": "True"}, state="exited"
    )

    adopted = await rig.fresh().actions.adopt(
        ADMIN, ENV_B, name=PROJECT, prepared=report.outcome, as_is=True
    )

    assert adopted.name == PROJECT


async def test_a_working_directory_the_host_will_not_mount_falls_back_to_the_containers(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    host.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    add_project_container(host, 1, compose_labels(), state="running")
    host.fail("create", 400)

    report = await rig.fresh().reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert isinstance(report.outcome, HostReadResult)
    assert report.outcome.file_source == "generated"
    assert report.attempted == NeedsFile(
        "working_directory_unmountable", "com.docker.compose.project.working_dir"
    )


async def test_a_host_read_is_refused_while_dockmins_own_mounts_are_not_known(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    host = rig.host
    assert isinstance(host, ReaderEngine)
    host.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    add_project_container(host, 1, compose_labels(), state="running")
    identity = SelfIdentity(
        container_id=OWN_CONTAINER,
        verified=(HomeEnvironment(ENV_B, "unix_socket", "dockmin"),),
    )

    report = await services_with_identity(rig, identity).reader.read_compose(ADMIN, ENV_B, PROJECT)

    assert report.attempted == NeedsFile("identity_unverified", "self_identity")
    assert isinstance(report.outcome, HostReadResult) and report.outcome.file_source == "generated"
    assert host.verbs() == [] and report.helper_container is None


async def test_a_namespace_whose_service_was_updated_after_the_read_is_refused_when_adopted(
    defaulting_stack_rig: Rig, tmp_path: Path
) -> None:
    rig = defaulting_stack_rig
    daemon = rig.host
    assert isinstance(daemon, StackDaemon)
    await use_files_compose_for_swarm(rig, tmp_path / "files-compose")
    await create_stack(rig)
    await deploy_stack(rig)
    deployed = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME)
    await rig.fresh().swarm_actions.forget(ADMIN, ENV_A, NAME, stack_id=deployed.id)
    report = await rig.fresh().reader.read_swarm(ADMIN, ENV_A, NAME)
    assert isinstance(report.outcome, HostReadResult)
    next(iter(daemon.services.values()))["Version"]["Index"] += 1

    with pytest.raises(ChangedSinceReview):
        await rig.fresh().swarm_adopter.adopt(
            ADMIN, ENV_A, NAME, prepared=report.outcome, as_is=True
        )

    with pytest.raises(ResourceNotFound):
        await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME)
