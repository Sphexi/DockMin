"""Container creation through the creation service over a fake daemon."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

import app.docker_engine.resources as resources
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.services import container_provisioning
from app.services.access import ResourceKey
from app.services.container_provisioning import (
    ConfigurationRejected,
    NameInUse,
    PullFailed,
    RestrictedOptions,
    SubmissionRejected,
)
from app.services.container_spec import spec_from_form, to_engine_body
from app.services.object_inspector import EnvironmentUnavailable, ObjectTooLarge
from app.services.resource_controls import ResourceControlService
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control, volume_key
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.docker_engine.container_engine import ENGINE_TEXT, ContainerEngine, created_id
from tests.docker_engine.fake_engine import Refuse, Stall
from tests.environment_support import activity_rows
from tests.provisioning_support import (
    AUTH,
    CREDENTIAL_SET_ID,
    IMAGE,
    ResolverStub,
    create_form,
    creation_with,
)
from tests.services.conftest import ContainerRig

FIRST = created_id(1)
SECRET = "hunter2-env-value"
REGISTRY_TEXT = "registry detail"
SOURCE = container_id(0xB7)
GHOST = container_id(0xFE)
PULLED_IMAGE = "ghcr.io/org/new:2"
FRONT_NUMBER, BACK_NUMBER = 0xF1, 0xF2
KIB_64 = 64 * 1024
SHORT_WAIT = 0.05


@pytest.fixture
def stocked(rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
    host.add_image(IMAGE)
    return rig


async def other_tasks() -> None:
    await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}), return_exceptions=True)


async def created_rows(rig: ContainerRig) -> list[dict[str, Any]]:
    return [r for r in await activity_rows(rig.db) if r["action"] == "container_created"]


async def control_of(rig: ContainerRig, container: str) -> Any:
    return await rig.access.controls.get(ADMIN, ResourceKey(ENV_A, "container", container))


async def grant_write(rig: ContainerRig, key: ResourceKey) -> None:
    await control(rig.access, key, public=True, level="write")


ADMIN_FORM = create_form(
    name="everything",
    command="serve --port 80",
    entrypoint="/bin/tini --",
    working_dir="/srv",
    user="1000",
    publish_all="on",
    network_mode="networks",
    restart_policy="on-failure",
    restart_max_retries="3",
    cpus="1.5",
    cpu_shares="512",
    memory="512m",
    memory_swap="1g",
    pids_limit="100",
    shm_size="64m",
    cap_add="net_admin",
    cap_drop="all",
    dns_servers="1.1.1.1",
    dns_search="example.test",
    dns_options="ndots:2",
    healthcheck_mode="command",
    healthcheck_test="curl -f localhost",
    healthcheck_interval="30s",
    healthcheck_retries="3",
    log_driver="syslog",
    runtime="nvidia",
    init="on",
    privileged="on",
    pid_host="on",
    ipc_host="on",
    **{
        "env-0-key": "DB_PASSWORD",
        "env-0-value": SECRET,
        "label-0-key": "tier",
        "label-0-value": "front",
        "port-0-container_port": "80",
        "port-0-host_port": "8080",
        "port-0-host_ip": "127.0.0.1",
        "mount-0-type": "volume",
        "mount-0-source": "data",
        "mount-0-target": "/data",
        "mount-1-type": "tmpfs",
        "mount-1-target": "/tmp",
        "mount-1-tmpfs_size": "64m",
        "mount-2-type": "bind",
        "mount-2-source": "/srv/host",
        "mount-2-target": "/host",
        "network-0-name": "front",
        "network-0-aliases": "web",
        "network-1-name": "back",
        "device-0-host_path": "/dev/fuse",
        "host-0-name": "db",
        "host-0-address": "10.0.0.5",
        "logopt-0-key": "syslog-address",
        "logopt-0-value": "tcp://logs.example.test:514",
    },
)


async def test_an_administrator_creates_with_every_option_group_and_owns_the_container(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    front = host.add_network("front", FRONT_NUMBER)
    back = host.add_network("back", BACK_NUMBER)

    outcome = await stocked.services.creation.create(ADMIN, ENV_A, ADMIN_FORM)

    sent = host.bodies()[0]
    expected = to_engine_body(
        spec_from_form(ADMIN_FORM), network_ids={"front": front, "back": back}
    )
    assert sent == expected
    assert host.calls("POST", "/containers/create")[0].query == {"name": "everything"}
    assert host.verbs() == ["create", "start"]
    assert (outcome.container_id, outcome.started, outcome.control_created) == (FIRST, True, True)
    owner = await control_of(stocked, FIRST)
    assert (owner.owner_user_id, owner.access_level, owner.is_public) == (ADMIN_ID, "write", False)
    [row] = await created_rows(stocked)
    assert (row["result"], row["actor_user_id"], row["target_id"]) == ("success", ADMIN_ID, FIRST)
    assert row["environment_id"] == ENV_A
    assert {k: row["detail"][k] for k in ("name", "image", "pull", "pulled", "started")} == {
        "name": "everything",
        "image": IMAGE,
        "pull": "missing",
        "pulled": False,
        "started": True,
    }
    assert row["detail"]["credential_set_id"] is None
    assert set(row["detail"]["restricted"].split(",")) == {
        "privileged",
        "cap_add",
        "bind_mount",
        "pid_namespace",
        "ipc_namespace",
        "devices",
        "runtime",
        "log_driver",
    }
    assert SECRET not in str(row)


async def test_a_standard_user_creates_with_the_options_open_to_them_and_owns_the_result(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    network = host.add_network("appnet", FRONT_NUMBER)
    host.add_volume("data")
    host.add_volume("docs")
    await grant_write(stocked, volume_key("data"))
    await control(stocked.access, volume_key("docs"), public=True, level="read")
    await grant_write(stocked, ResourceKey(ENV_A, "network", network))
    form = create_form(
        name="mine",
        network_mode="networks",
        cap_drop="net_raw",
        init="on",
        restart_policy="always",
        memory="256m",
        pids_limit="50",
        log_driver="local",
        healthcheck_mode="command",
        healthcheck_test="true",
        dns_servers="1.1.1.1",
        **{
            "port-0-container_port": "80",
            "port-0-host_port": "8080",
            "label-0-key": "tier",
            "label-0-value": "front",
            "env-0-key": "MODE",
            "env-0-value": "fast",
            "mount-0-source": "data",
            "mount-0-target": "/data",
            "mount-1-source": "docs",
            "mount-1-target": "/docs",
            "mount-1-read_only": "on",
            "mount-2-source": "fresh",
            "mount-2-target": "/fresh",
            "mount-3-type": "tmpfs",
            "mount-3-target": "/tmp",
            "network-0-name": "appnet",
            "network-0-aliases": "web",
            "logopt-0-key": "max-size",
            "logopt-0-value": "10m",
        },
    )

    outcome = await stocked.services.creation.create(STANDARD, ENV_A, form)

    body = host.bodies()[0]
    assert list(body["NetworkingConfig"]["EndpointsConfig"]) == [network]
    assert body["HostConfig"]["NetworkMode"] == network
    assert "fresh" in host.volumes
    owner = await control_of(stocked, outcome.container_id)
    assert (owner.owner_user_id, owner.access_level, owner.is_public) == (
        STANDARD_ID,
        "write",
        False,
    )
    assert (owner.user_ids, owner.team_ids, owner.is_administrators_only) == ((), (), False)
    await stocked.access.authoriser.require_access(
        STANDARD, ResourceKey(ENV_A, "container", outcome.container_id), "write"
    )
    [row] = await created_rows(stocked)
    assert (row["result"], row["actor_user_id"]) == ("success", STANDARD_ID)
    assert "restricted" not in row["detail"]


@pytest.mark.parametrize("who", [ADMIN, STANDARD], ids=["administrator", "standard"])
async def test_a_stack_label_in_any_case_is_refused_beside_its_row_and_nothing_is_created(
    stocked: ContainerRig, host: ContainerEngine, who: Principal
) -> None:
    form = create_form(**{"label-1-key": "Com.Docker.Compose.Project", "label-1-value": "shop"})

    with pytest.raises(SubmissionRejected) as refused:
        await stocked.services.creation.create(who, ENV_A, form)

    assert set(refused.value.fields) == {"label-1-key"}
    assert refused.value.state is not None
    assert refused.value.state.get("label-1-value") == "shop"
    assert host.mutations() == []
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")
    assert row["detail"]["fields"] == "label-1-key"


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        pytest.param({"name": "ab" * 32}, "name", id="64-hex-name"),
        pytest.param(
            {"label-0-key": "k", "label-0-value": "v" * KIB_64}, "labels", id="label-size-cap"
        ),
        pytest.param({"image": "NOT A REFERENCE"}, "image", id="image"),
        pytest.param({"memory": "1m"}, "memory", id="memory"),
    ],
)
async def test_an_invalid_submission_is_refused_by_the_service_with_the_field_and_a_failure_row(
    stocked: ContainerRig, host: ContainerEngine, fields: dict[str, str], key: str
) -> None:
    with pytest.raises(SubmissionRejected) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, create_form(**fields))

    assert set(refused.value.fields) == {key}
    assert host.mutations() == []
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == (
        "failure",
        "invalid",
        None,
    )


RESTRICTED_FORMS = [
    pytest.param({"privileged": "on"}, "privileged", "privileged", id="privileged"),
    pytest.param({"cap_add": "NET_ADMIN"}, "cap_add", "cap_add", id="added-capability"),
    pytest.param(
        {"mount-0-type": "bind", "mount-0-source": "/etc", "mount-0-target": "/x"},
        "bind_mount",
        "mount-0-type",
        id="bind-mount",
    ),
    pytest.param({"network_mode": "host"}, "network_host", "network_mode", id="host-network"),
    pytest.param({"pid_host": "on"}, "pid_namespace", "pid_host", id="host-pid"),
    pytest.param({"ipc_host": "on"}, "ipc_namespace", "ipc_host", id="host-ipc"),
    pytest.param(
        {"device-0-host_path": "/dev/mem"}, "devices", "device-0-host_path", id="device-mapping"
    ),
    pytest.param({"runtime": "nvidia"}, "runtime", "runtime", id="non-default-runtime"),
    pytest.param({"log_driver": "syslog"}, "log_driver", "log_driver", id="log-driver"),
    pytest.param(
        {"log_driver": "local", "logopt-2-key": "syslog-address", "logopt-2-value": "tcp://x"},
        "log_driver",
        "logopt-2-key",
        id="log-option",
    ),
]


@pytest.mark.parametrize(("fields", "option", "key"), RESTRICTED_FORMS)
async def test_a_standard_user_who_bypasses_the_form_is_refused_each_restricted_option(
    stocked: ContainerRig, host: ContainerEngine, fields: dict[str, str], option: str, key: str
) -> None:
    with pytest.raises(RestrictedOptions) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, create_form(**fields))

    assert refused.value.refused == (option,)
    assert key in refused.value.fields
    assert refused.value.state is not None and key in refused.value.state.errors
    assert host.mutations() == []
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == (
        "failure",
        "restricted_option",
        None,
    )
    assert row["detail"]["refused"] == option
    assert await control_of(stocked, FIRST) is None


async def test_a_standard_user_who_bypasses_the_form_is_refused_every_restricted_option_at_once(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    every = {
        k: v for param in RESTRICTED_FORMS for k, v in cast(dict[str, Any], param.values[0]).items()
    }
    every["log_driver"] = "syslog"
    every.pop("logopt-2-key")
    every.pop("logopt-2-value")

    with pytest.raises(RestrictedOptions) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, create_form(**every))

    assert set(refused.value.refused) == {
        "privileged",
        "cap_add",
        "bind_mount",
        "network_host",
        "pid_namespace",
        "ipc_namespace",
        "devices",
        "runtime",
        "log_driver",
    }
    assert host.mutations() == []


async def test_a_hidden_volume_and_unreachable_networks_are_field_errors_that_disclose_nothing(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("locked")
    host.add_volume("shared")
    await control(stocked.access, volume_key("shared"), public=True, level="read")
    open_network = host.add_network("opennet", FRONT_NUMBER)
    host.add_network("lockednet", BACK_NUMBER)
    await grant_write(stocked, ResourceKey(ENV_A, "network", open_network))
    form = create_form(
        network_mode="networks",
        **{
            "mount-0-source": "locked",
            "mount-0-target": "/a",
            "mount-1-source": "shared",
            "mount-1-target": "/b",
            "mount-2-source": "shared",
            "mount-2-target": "/c",
            "mount-2-read_only": "on",
            "network-0-name": "lockednet",
            "network-1-name": "ghost",
            "network-2-name": "opennet",
            "network-3-name": open_network,
        },
    )

    with pytest.raises(SubmissionRejected) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, form)

    assert set(refused.value.fields) == {
        "mount-0-source",
        "mount-1-source",
        "network-0-name",
        "network-1-name",
        "network-3-name",
    }
    assert refused.value.fields["network-0-name"] == refused.value.fields["network-1-name"]
    assert host.mutations() == []
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")


async def test_a_host_backed_volume_and_a_host_driver_network_are_refused_even_with_access(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("bound", options={"o": "bind", "device": "/"})
    host.add_volume("plugin", driver="rexray")
    host_network = host.add_network("hostlike", FRONT_NUMBER, driver="host")
    await grant_write(stocked, volume_key("bound"))
    await grant_write(stocked, volume_key("plugin"))
    await grant_write(stocked, ResourceKey(ENV_A, "network", host_network))
    form = create_form(
        network_mode="networks",
        **{
            "mount-0-source": "bound",
            "mount-0-target": "/a",
            "mount-1-source": "plugin",
            "mount-1-target": "/b",
            "network-0-name": "hostlike",
        },
    )

    with pytest.raises(RestrictedOptions) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, form)

    assert refused.value.refused == ("host_backed_volume", "network_host")
    assert {"mount-0-source", "mount-1-source", "network-0-name"} <= set(refused.value.fields)
    assert host.mutations() == []


async def test_an_administrator_may_use_a_host_backed_volume_and_a_host_driver_network(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("bound", options={"o": "bind", "device": "/"})
    host.add_network("hostlike", FRONT_NUMBER, driver="host")
    form = create_form(
        network_mode="networks",
        **{"mount-0-source": "bound", "mount-0-target": "/a", "network-0-name": "hostlike"},
    )

    outcome = await stocked.services.creation.create(ADMIN, ENV_A, form)

    assert outcome.started
    assert host.verbs() == ["create", "start"]


PULL_POLICIES = [
    pytest.param("missing", IMAGE, ["create", "start"], id="missing-and-present"),
    pytest.param("missing", PULLED_IMAGE, ["pull", "create", "start"], id="missing-and-absent"),
    pytest.param("always", IMAGE, ["pull", "create", "start"], id="always-and-present"),
    pytest.param("never", IMAGE, ["create", "start"], id="never-and-present"),
]


@pytest.mark.parametrize(("policy", "image", "verbs"), PULL_POLICIES)
async def test_the_pull_policy_decides_whether_the_image_is_pulled_before_the_create(
    stocked: ContainerRig, host: ContainerEngine, policy: str, image: str, verbs: list[str]
) -> None:
    outcome = await stocked.services.creation.create(
        ADMIN, ENV_A, create_form(pull=policy, image=image)
    )

    assert host.verbs() == verbs
    assert outcome.pulled is (verbs[0] == "pull")
    [row] = await created_rows(stocked)
    assert (row["detail"]["pull"], row["detail"]["pulled"]) == (policy, verbs[0] == "pull")


async def test_never_pulling_an_image_the_host_lacks_is_refused_beside_the_image_field(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(SubmissionRejected) as refused:
        await rig.services.creation.create(ADMIN, ENV_A, create_form(pull="never"))

    assert refused.value.fields == {"image": "The image is not on this host."}
    assert host.mutations() == []
    [row] = await created_rows(rig)
    assert row["detail"]["reason"] == "image_missing"


async def test_a_pull_uses_the_credentials_the_resolver_returns_and_the_row_records_the_set(
    rig: ContainerRig, host: ContainerEngine, clock: FakeClock
) -> None:
    resolver = ResolverStub()
    service = creation_with(rig, clock, resolver)

    await service.create(ADMIN, ENV_A, create_form(image=PULLED_IMAGE))

    assert resolver.asked == [(ENV_A, PULLED_IMAGE)]
    pull = host.calls("POST", "/images/create")[0]
    assert pull.headers["x-registry-auth"] == AUTH
    [row] = await created_rows(rig)
    assert row["detail"]["credential_set_id"] == CREDENTIAL_SET_ID
    assert AUTH not in str(await activity_rows(rig.db))


async def test_the_resolver_is_not_asked_when_the_image_is_already_on_the_host(
    stocked: ContainerRig, host: ContainerEngine, clock: FakeClock
) -> None:
    resolver = ResolverStub()
    service = creation_with(stocked, clock, resolver)

    await service.create(ADMIN, ENV_A, create_form())

    assert resolver.asked == []
    assert "x-registry-auth" not in str(host.requests)


async def test_a_failed_pull_creates_nothing_and_shows_no_registry_text(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.pull_status = 500

    with pytest.raises(PullFailed) as failed:
        await rig.services.creation.create(ADMIN, ENV_A, create_form())

    assert str(failed.value) == "The image could not be pulled."
    assert host.verbs() == ["pull"]
    [row] = await created_rows(rig)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == (
        "failure",
        "pull_failed",
        None,
    )
    assert ENGINE_TEXT not in str(row)


ENGINE_FAILURES = [
    pytest.param(
        lambda host: host.route("GET", f"/images/{IMAGE}/json", Refuse()),
        EnvironmentUnavailable,
        "unreachable",
        "The Docker host could not be reached.",
        id="image-check-unreachable",
    ),
    pytest.param(
        lambda host: host.add("c" * 64, "taken", image=IMAGE),
        NameInUse,
        "name_in_use",
        "A container with this name already exists.",
        id="name-in-use",
    ),
    pytest.param(
        lambda host: host.fail("create", 400),
        ConfigurationRejected,
        "engine_rejected",
        "Docker refused this configuration.",
        id="rejected",
    ),
    pytest.param(
        lambda host: host.route("POST", "/containers/create", Stall()),
        EnvironmentUnavailable,
        "outcome_unknown",
        "The Docker host did not answer in time. The action may still complete.",
        id="outcome-unknown",
    ),
    pytest.param(
        lambda host: host.route("POST", "/containers/create", Refuse()),
        EnvironmentUnavailable,
        "unreachable",
        "The Docker host could not be reached.",
        id="unreachable",
    ),
]


@pytest.mark.parametrize(("arrange", "error", "reason", "message"), ENGINE_FAILURES)
async def test_each_engine_failure_on_create_is_a_product_message_and_a_failure_row(
    stocked: ContainerRig,
    host: ContainerEngine,
    arrange: Any,
    error: type[Exception],
    reason: str,
    message: str,
) -> None:
    arrange(host)

    with pytest.raises(error) as failed:
        await stocked.services.creation.create(ADMIN, ENV_A, create_form(name="taken"))

    assert str(failed.value) == message
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == ("failure", reason, None)
    assert ENGINE_TEXT not in str(row)
    assert await control_of(stocked, FIRST) is None


async def test_a_pull_that_cannot_reach_the_host_is_reported_as_unreachable(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.route("POST", "/images/create", Refuse())

    with pytest.raises(EnvironmentUnavailable) as failed:
        await rig.services.creation.create(ADMIN, ENV_A, create_form())

    assert failed.value.kind == "unreachable"
    [row] = await created_rows(rig)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "unreachable")


async def test_a_volume_whose_description_is_over_the_read_limit_refuses_the_create(
    stocked: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.add_volume("data")
    monkeypatch.setattr(resources, "VOLUME_INSPECT_BYTES", 10)
    form = create_form(**{"mount-0-source": "data", "mount-0-target": "/data"})

    with pytest.raises(ObjectTooLarge):
        await stocked.services.creation.create(STANDARD, ENV_A, form)

    assert host.mutations() == []
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "too_large")


async def test_a_create_that_outlives_the_wait_after_a_cancel_is_settled_when_it_finishes(
    stocked: ContainerRig, host: ContainerEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_provisioning, "SETTLE_WAIT_SECONDS", SHORT_WAIT)
    gate = host.hold("create")
    task = asyncio.ensure_future(
        stocked.services.creation.create(STANDARD, ENV_A, create_form(name="slow"))
    )
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unknown = await created_rows(stocked)

    gate.release.set()
    await other_tasks()
    await other_tasks()

    [first] = unknown
    assert (first["result"], first["detail"]["reason"]) == ("failure", "outcome_unknown")
    settled = (await created_rows(stocked))[1]
    assert (settled["result"], settled["target_id"]) == ("success", FIRST)
    assert settled["detail"]["interrupted"] is True and settled["detail"]["settled_late"] is True
    assert (await control_of(stocked, FIRST)).owner_user_id == STANDARD_ID


async def test_a_container_that_cannot_start_is_kept_owned_and_recorded_as_a_success(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.fail("start", 500)

    outcome = await stocked.services.creation.create(STANDARD, ENV_A, create_form())

    assert (outcome.started, outcome.start_failed, outcome.control_created) == (False, True, True)
    assert host.verbs() == ["create", "start"]
    [row] = await created_rows(stocked)
    assert row["result"] == "success"
    assert (row["detail"]["started"], row["detail"]["start_failed"]) == (False, True)
    assert (await control_of(stocked, FIRST)).owner_user_id == STANDARD_ID


async def test_a_container_created_to_stay_stopped_is_never_started(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    form = create_form(start="")

    outcome = await stocked.services.creation.create(STANDARD, ENV_A, form)

    assert (outcome.started, outcome.start_failed) == (False, False)
    assert host.verbs() == ["create"]
    assert host.boxes[FIRST].state == "created"


async def test_a_control_that_cannot_be_saved_is_retried_once_and_then_the_container_is_owned(
    stocked: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ResourceControlService.claim

    async def broken(self: ResourceControlService, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database is busy")

    attempts = iter([broken, real])
    used: list[object] = []

    async def flaky(self: ResourceControlService, *args: Any, **kwargs: Any) -> bool:
        attempt = next(attempts)
        used.append(attempt)
        return await attempt(self, *args, **kwargs)

    monkeypatch.setattr(ResourceControlService, "claim", flaky)

    outcome = await stocked.services.creation.create(STANDARD, ENV_A, create_form())

    assert used == [broken, real]
    assert outcome.control_created
    assert (await control_of(stocked, FIRST)).owner_user_id == STANDARD_ID
    [row] = await created_rows(stocked)
    assert "control_created" not in row["detail"]


async def test_a_control_that_cannot_be_saved_leaves_the_container_and_records_the_gap(
    stocked: ContainerRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ResourceControlService, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database is busy")

    monkeypatch.setattr(ResourceControlService, "claim", broken)

    outcome = await stocked.services.creation.create(STANDARD, ENV_A, create_form())

    assert (outcome.container_id, outcome.control_created) == (FIRST, False)
    assert await control_of(stocked, FIRST) is None
    [row] = await created_rows(stocked)
    assert (row["result"], row["detail"]["control_created"]) == ("success", False)


@pytest.mark.parametrize(
    ("who", "source", "recorded"),
    [
        pytest.param(ADMIN, SOURCE, True, id="readable-source"),
        pytest.param(ADMIN, GHOST, False, id="absent-source"),
        pytest.param(STANDARD, SOURCE, False, id="unreadable-source"),
    ],
)
async def test_the_source_of_a_duplicate_is_recorded_only_when_the_caller_may_read_it(
    stocked: ContainerRig, host: ContainerEngine, who: Principal, source: str, recorded: bool
) -> None:
    host.add(SOURCE, "source", image=IMAGE)

    await stocked.services.creation.create(who, ENV_A, create_form(), duplicate_of=source)

    [row] = await created_rows(stocked)
    assert ("duplicate_of" in row["detail"]) is recorded


async def test_a_cancel_during_the_create_call_still_claims_the_container_and_writes_the_row(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("create")
    task = asyncio.ensure_future(
        stocked.services.creation.create(STANDARD, ENV_A, create_form(name="interrupted"))
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await created_rows(stocked)
    assert (row["result"], row["target_id"], row["detail"]["interrupted"]) == (
        "success",
        FIRST,
        True,
    )
    assert (await control_of(stocked, FIRST)).owner_user_id == STANDARD_ID


async def test_a_cancel_during_a_pull_writes_an_interrupted_failure_and_creates_nothing(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("pull")
    task = asyncio.ensure_future(rig.services.creation.create(ADMIN, ENV_A, create_form()))
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    [row] = await created_rows(rig)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == (
        "failure",
        "interrupted",
        None,
    )
    assert host.verbs() == ["pull"]


async def test_a_cancel_before_anything_was_sent_to_the_host_writes_no_row(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("info")
    task = asyncio.ensure_future(
        stocked.services.creation.create(ADMIN, ENV_A, create_form(runtime="nvidia"))
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await created_rows(stocked) == []
    assert host.mutations() == []


async def test_reviewing_changes_nothing_and_writes_no_row(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    review = await stocked.services.creation.review(
        STANDARD, ENV_A, create_form(name="web", **{"env-0-key": "A", "env-0-value": "1"})
    )

    assert (review.image, review.name, review.will_pull, review.start) == (
        IMAGE,
        "web",
        False,
        True,
    )
    assert [s.key for s in review.sections][:1] == ["environment"]
    assert review.restricted_used == ()
    assert host.mutations() == []
    assert await activity_rows(stocked.db) == []


async def test_reviewing_a_restricted_option_as_an_administrator_names_it_without_a_row(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    review = await stocked.services.creation.review(
        ADMIN, ENV_A, create_form(privileged="on", cap_add="net_admin")
    )

    assert review.restricted_used == ("privileged mode", "added capabilities")
    assert await activity_rows(stocked.db) == []


async def test_reviewing_a_restricted_option_as_a_standard_user_is_refused_without_a_row(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(RestrictedOptions) as refused:
        await stocked.services.creation.review(STANDARD, ENV_A, create_form(privileged="on"))

    assert refused.value.refused == ("privileged",)
    assert await activity_rows(stocked.db) == []


async def test_a_blank_form_ticks_start_and_defaults_the_pull_policy_and_network(
    stocked: ContainerRig,
) -> None:
    context = await stocked.services.creation.form_context(STANDARD, ENV_A)

    assert context.state.checked("start")
    assert (context.state.get("pull"), context.state.get("network_mode")) == ("missing", "bridge")
    assert (context.is_local, context.is_administrator, context.duplicate_of) == (
        False,
        False,
        None,
    )


SOURCE_BODY = {
    "Image": IMAGE,
    "Env": [f"DB_PASSWORD={SECRET}", "MODE=fast"],
    "Cmd": ["serve", "--fast"],
    "Labels": {"tier": "front", "com.docker.compose.project": "shop", "db_password": SECRET},
    "HostConfig": {
        "Binds": ["data:/data"],
        "Mounts": [{"Type": "bind", "Source": "/srv/host", "Target": "/host"}],
        "Privileged": True,
    },
}


async def test_a_standard_users_duplicate_form_is_redacted_the_way_the_detail_page_is(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("data")
    host.add_configured(SOURCE, "source", SOURCE_BODY)
    await control(
        stocked.access, ResourceKey(ENV_A, "container", SOURCE), public=True, level="read"
    )

    context = await stocked.services.creation.form_context(STANDARD, ENV_A, source_id=SOURCE)

    values = context.state.values
    assert (context.duplicate_of, context.source_name, context.suggested_name) == (
        SOURCE,
        "source",
        "source-copy",
    )
    assert values["image"] == IMAGE and not values.get("name")
    assert not any(k.startswith("env-") for k in values)
    assert not values.get("command") and not values.get("privileged")
    assert values["label-0-key"] == "tier" and not any(
        v in (SECRET, "/srv/host", "shop") for v in values.values()
    )
    kinds = {notice.kind for notice in context.notices}
    assert {"labels_dropped", "restricted", "withheld"} <= kinds
    assert host.mutations() == []


async def test_an_administrators_duplicate_form_carries_everything_but_the_stack_labels(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("data")
    host.add_configured(SOURCE, "source", SOURCE_BODY)

    context = await stocked.services.creation.form_context(ADMIN, ENV_A, source_id=SOURCE)

    values = context.state.values
    assert values["env-0-key"] == "DB_PASSWORD" and values["env-0-value"] == SECRET
    assert values["privileged"] == "on"
    assert "bind" in values.values()
    assert "com.docker.compose.project" not in values.values()


async def test_a_duplicate_form_for_a_source_the_caller_cannot_read_is_not_found(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_configured(SOURCE, "source", SOURCE_BODY)

    with pytest.raises(ResourceNotFound):
        await stocked.services.creation.form_context(STANDARD, ENV_A, source_id=SOURCE)


@pytest.mark.parametrize("operation", ["create", "review", "form", "duplicate"])
async def test_a_standard_user_cannot_create_on_the_local_socket_and_nothing_is_recorded(
    stocked: ContainerRig, host: ContainerEngine, operation: str
) -> None:
    host.add(SOURCE, "source", image=IMAGE)
    service = stocked.services.creation
    attempts: dict[str, Callable[[], Awaitable[object]]] = {
        "create": lambda: service.create(STANDARD, ENV_B, create_form()),
        "review": lambda: service.review(STANDARD, ENV_B, create_form()),
        "form": lambda: service.form_context(STANDARD, ENV_B),
        "duplicate": lambda: service.form_context(STANDARD, ENV_B, source_id=SOURCE),
    }

    with pytest.raises(PermissionDenied):
        await attempts[operation]()

    assert host.requests == []
    assert await activity_rows(stocked.db) == []


async def test_an_administrator_creates_on_the_local_socket(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    outcome = await stocked.services.creation.create(ADMIN, ENV_B, create_form())

    assert outcome.started
    [row] = await created_rows(stocked)
    assert row["environment_id"] == ENV_B


async def test_an_unknown_environment_is_not_found_and_leaves_no_row(
    stocked: ContainerRig,
) -> None:
    with pytest.raises(ResourceNotFound):
        await stocked.services.creation.create(STANDARD, 999, create_form())

    assert await activity_rows(stocked.db) == []
