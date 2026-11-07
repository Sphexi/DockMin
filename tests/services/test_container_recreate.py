"""Container recreation through the recreation service over a fake daemon."""

import asyncio
from typing import Any

import pytest

import app.docker_engine.resources as resources
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services import container_provisioning
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.container_actions import ContainerGone, ContainerStateConflict
from app.services.container_provisioning import (
    NameChanged,
    NotRecreatable,
    PullFailed,
    RecreateFailed,
    RestrictedOptions,
)
from app.services.container_restrictions import OPTION_IDS
from app.services.object_inspector import ContainerTooLarge, EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control, volume_key
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.docker_engine.container_engine import ENGINE_TEXT, Box, ContainerEngine, created_id
from tests.docker_engine.fake_engine import Refuse, Stall
from tests.environment_support import activity_rows
from tests.provisioning_support import (
    AUTH,
    CREDENTIAL_SET_ID,
    IMAGE,
    NETWORK,
    OLD,
    OLD_ENV,
    VOLUME,
    ResolverStub,
    anonymous_volume_of,
    recreation_with,
    seed_recreatable,
)
from tests.services.conftest import ContainerRig
from tests.support import present

NEW = created_id(1)
PARKED = f"web-recreating-{OLD[:12]}"
GHOST = container_id(0xFE)
SHORT_WAIT = 0.05
NEW_IMAGE_ID = "sha256:" + "d" * 64
FULL_SEQUENCE = ["stop", "rename", "create", "start", "remove"]
OLD_KEY = ResourceKey(ENV_A, "container", OLD)
NEW_KEY = ResourceKey(ENV_A, "container", NEW)


def state_of_original(host: ContainerEngine) -> tuple[bool, str | None, str | None]:
    box = host.boxes.get(OLD)
    return (box is not None, None if box is None else box.name, None if box is None else box.state)


async def other_tasks() -> None:
    await asyncio.gather(*(asyncio.all_tasks() - {asyncio.current_task()}), return_exceptions=True)


async def recreated_rows(rig: ContainerRig) -> list[dict[str, Any]]:
    return [r for r in await activity_rows(rig.db) if r["action"] == "container_recreated"]


async def claim_for_standard(rig: ContainerRig) -> None:
    async with rig.db.transaction():
        await rig.access.controls.claim(OLD_KEY, STANDARD_ID)


async def control_of(rig: ContainerRig, key: ResourceKey) -> Any:
    return await rig.access.controls.get(ADMIN, key)


@pytest.fixture
def recreatable(rig: ContainerRig, host: ContainerEngine) -> Box:
    return seed_recreatable(host)


@pytest.fixture
async def owned(rig: ContainerRig, recreatable: Box) -> Box:
    await claim_for_standard(rig)
    return recreatable


async def test_recreate_keeps_configuration_networks_and_volumes_and_the_row_says_nothing_changed(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    network_id = host.networks[next(k for k, v in host.networks.items() if v["Name"] == NETWORK)][
        "Id"
    ]
    anonymous = anonymous_volume_of(owned)
    await control(rig.access, volume_key(VOLUME), administrators_only=True)
    await control(rig.access, ResourceKey(ENV_A, "network", network_id), administrators_only=True)
    before = (
        present(owned.config)["Env"],
        present(owned.config)["Labels"],
        present(owned.config)["Cmd"],
        present(owned.host_config)["RestartPolicy"],
        present(owned.host_config)["Memory"],
        present(owned.host_config)["Binds"],
    )
    volumes_before = set(host.volumes)

    outcome = await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    renewed = host.boxes[outcome.new_id]
    assert OLD not in host.boxes and len(host.boxes) == 1
    assert (renewed.name, renewed.state, renewed.image_id) == ("web", "running", owned.image_id)
    assert (
        present(renewed.config)["Env"],
        present(renewed.config)["Labels"],
        present(renewed.config)["Cmd"],
        present(renewed.host_config)["RestartPolicy"],
        present(renewed.host_config)["Memory"],
        present(renewed.host_config)["Binds"],
    ) == before
    attachment = present(renewed.endpoints)[NETWORK]
    assert attachment["NetworkID"] == network_id and "web" in attachment["Aliases"]
    assert {m["Name"] for m in renewed.mounts} == {VOLUME, anonymous}
    assert set(host.volumes) == volumes_before
    assert host.verbs() == FULL_SEQUENCE
    assert (outcome.old_id, outcome.new_id, outcome.name) == (OLD, NEW, "web")
    assert (outcome.started, outcome.old_removed, outcome.kept_as) == (True, True, None)
    assert (outcome.pulled, outcome.image_changed, outcome.changed) == (False, False, ("none",))
    assert (outcome.control_moved, outcome.old_control_kept) == (True, False)
    assert len(rig.locks) == 0


async def test_recreate_row_names_the_container_acted_on_the_new_id_and_what_changed(
    rig: ContainerRig, owned: Box
) -> None:
    await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    [row] = await recreated_rows(rig)
    assert (row["result"], row["actor_user_id"], row["target_id"]) == ("success", STANDARD_ID, OLD)
    assert row["environment_id"] == ENV_A
    detail = row["detail"]
    assert {k: detail[k] for k in ("name", "image", "new_container_id")} == {
        "name": "web",
        "image": IMAGE,
        "new_container_id": NEW,
    }
    assert (detail["changed"], detail["image_changed"], detail["old_removed"]) == (
        "none",
        False,
        True,
    )
    assert (detail["started"], detail["pulled"], detail["credential_set_id"]) == (True, False, None)
    assert detail["old_image_id"] == detail["new_image_id"]
    assert "APP=1" not in str(row)


async def test_recreate_moves_the_access_control_to_the_new_container(
    rig: ContainerRig, owned: Box
) -> None:
    await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    moved = await control_of(rig, NEW_KEY)
    assert (moved.owner_user_id, moved.access_level) == (STANDARD_ID, "write")
    assert await control_of(rig, OLD_KEY) is None


async def test_recreate_of_an_uncontrolled_container_leaves_the_new_one_uncontrolled(
    rig: ContainerRig, recreatable: Box
) -> None:
    outcome = await rig.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

    assert outcome.control_moved
    assert await control_of(rig, NEW_KEY) is None


async def test_recreate_with_pull_uses_the_resolved_credentials_and_names_the_new_image(
    rig: ContainerRig, host: ContainerEngine, owned: Box, clock: FakeClock
) -> None:
    host.pull_id = NEW_IMAGE_ID
    host.pull_config = {"Env": ["PATH=/bin", "FOO=new"], "Cmd": ["nginx"]}
    resolver = ResolverStub()
    service = recreation_with(rig, clock, resolver)

    outcome = await service.recreate(STANDARD, ENV_A, OLD, expected_name="web", pull=True)

    assert host.verbs() == ["pull", *FULL_SEQUENCE]
    assert host.calls("POST", "/images/create")[0].headers["x-registry-auth"] == AUTH
    assert resolver.asked == [(ENV_A, IMAGE)]
    assert (outcome.pulled, outcome.image_changed) == (True, True)
    assert outcome.changed == ("image", "env")
    [row] = await recreated_rows(rig)
    assert (row["detail"]["pulled"], row["detail"]["credential_set_id"]) == (
        True,
        CREDENTIAL_SET_ID,
    )
    assert row["detail"]["new_image_id"] == NEW_IMAGE_ID != row["detail"]["old_image_id"]
    assert row["detail"]["changed"] == "image,env"
    assert AUTH not in str(await activity_rows(rig.db))
    renewed = host.boxes[outcome.new_id]
    assert (
        "FOO=new" in present(renewed.config)["Env"]
        and OLD_ENV not in present(renewed.config)["Env"]
    )
    assert "APP=1" in present(renewed.config)["Env"]


async def test_recreate_without_pull_does_not_ask_the_resolver_or_touch_the_registry(
    rig: ContainerRig, host: ContainerEngine, owned: Box, clock: FakeClock
) -> None:
    resolver = ResolverStub()
    service = recreation_with(rig, clock, resolver)

    await service.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert resolver.asked == []
    assert host.calls("POST", "/images/create") == []


async def test_a_new_local_image_under_the_same_reference_is_reported_as_an_image_change(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    host.add_image(IMAGE, image_id=NEW_IMAGE_ID, config={"Env": ["PATH=/bin", OLD_ENV]})

    outcome = await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert (outcome.image_changed, outcome.pulled) == (True, False)
    assert "image" in outcome.changed


async def test_a_name_that_changed_since_review_is_refused_before_anything_is_touched(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    with pytest.raises(NameChanged):
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="other")

    assert host.verbs() == []
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "name_changed")
    assert len(rig.locks) == 0


@pytest.mark.parametrize(
    "container", [OLD, GHOST, "web", OLD.upper()], ids=["unseen", "absent", "name", "case"]
)
async def test_a_container_the_caller_cannot_see_is_not_found_whether_or_not_it_exists(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box, container: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await rig.services.recreation.recreate(STANDARD, ENV_A, container, expected_name="web")

    assert host.verbs() == []
    assert await activity_rows(rig.db) == []


async def test_an_administrator_asking_for_an_absent_container_is_told_it_no_longer_exists(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(ContainerGone):
        await rig.services.recreation.recreate(ADMIN, ENV_A, GHOST, expected_name="web")

    assert host.verbs() == []
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["reason"], row["target_id"]) == (
        "failure",
        "not_found",
        GHOST,
    )


async def test_a_holder_of_read_access_cannot_recreate_and_no_row_is_written(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box
) -> None:
    await control(rig.access, OLD_KEY, public=True, level="read")

    with pytest.raises(PermissionDenied):
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert host.verbs() == []
    assert await recreated_rows(rig) == []


async def test_a_standard_user_cannot_recreate_on_the_local_socket_even_with_a_write_grant(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box
) -> None:
    key = ResourceKey(ENV_B, "container", OLD)
    await control(rig.access, key, public=True, level="write")

    with pytest.raises(PermissionDenied):
        await rig.services.recreation.recreate(STANDARD, ENV_B, OLD, expected_name="web")

    assert host.verbs() == []
    assert await recreated_rows(rig) == []


async def test_an_administrator_recreates_on_the_local_socket(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box
) -> None:
    outcome = await rig.services.recreation.recreate(ADMIN, ENV_B, OLD, expected_name="web")

    assert outcome.old_removed
    [row] = await recreated_rows(rig)
    assert row["environment_id"] == ENV_B


BLOCKED = [
    pytest.param({"host_config": {"AutoRemove": True}}, False, ("auto_remove",), id="auto-remove"),
    pytest.param(
        {"config": {"Labels": {"com.docker.swarm.service.id": "x"}}},
        False,
        ("swarm_managed",),
        id="swarm-task",
    ),
    pytest.param({"name": "ab" * 32}, False, ("hex_name",), id="64-hex-name"),
    pytest.param({"config": {"Image": NEW_IMAGE_ID}}, True, ("image_id_reference",), id="image-id"),
]


@pytest.mark.parametrize(("seed", "pull", "blockers"), BLOCKED)
async def test_a_container_that_cannot_be_recreated_is_refused_with_every_reason_and_a_row(
    rig: ContainerRig,
    host: ContainerEngine,
    seed: dict[str, Any],
    pull: bool,
    blockers: tuple[str, ...],
) -> None:
    host.add_image(NEW_IMAGE_ID, image_id=NEW_IMAGE_ID)
    box = seed_recreatable(host, **seed)

    with pytest.raises(NotRecreatable) as refused:
        await rig.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name=box.name, pull=pull)

    assert refused.value.blockers == blockers
    assert host.verbs() == []
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "not_recreatable")
    assert row["detail"]["blockers"] == ",".join(blockers)


BODY_OPTIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("privileged", "host", {"Privileged": True}),
    ("cap_add", "host", {"CapAdd": ["NET_ADMIN"]}),
    ("bind_mount", "host", {"Binds": ["/etc:/x"]}),
    ("volume_driver", "host", {"VolumeDriver": "rexray"}),
    ("mount_type", "host", {"Mounts": [{"Type": "image", "Source": "img", "Target": "/i"}]}),
    ("volumes_from", "host", {"VolumesFrom": ["other"]}),
    ("network_host", "host", {"NetworkMode": "host"}),
    ("network_container", "host", {"NetworkMode": "container:" + "9" * 64}),
    ("pid_namespace", "host", {"PidMode": "host"}),
    ("ipc_namespace", "host", {"IpcMode": "host"}),
    ("uts_namespace", "host", {"UTSMode": "host"}),
    ("userns_mode", "host", {"UsernsMode": "host"}),
    ("cgroupns_mode", "host", {"CgroupnsMode": "host"}),
    ("cgroup", "host", {"Cgroup": "x"}),
    ("cgroup_parent", "host", {"CgroupParent": "/x"}),
    ("devices", "host", {"Devices": [{"PathOnHost": "/dev/mem", "PathInContainer": "/dev/mem"}]}),
    ("security_opt", "host", {"SecurityOpt": ["seccomp=unconfined"]}),
    ("masked_paths", "host", {"MaskedPaths": []}),
    ("sysctls", "host", {"Sysctls": {"net.ipv4.ip_forward": "1"}}),
    ("runtime", "host", {"Runtime": "nvidia"}),
    ("log_driver", "host", {"LogConfig": {"Type": "syslog", "Config": {}}}),
    ("links", "host", {"Links": ["a:b"]}),
    ("oom_kill_disable", "host", {"OomKillDisable": True}),
    ("oom_score_adj", "host", {"OomScoreAdj": -500}),
    ("realtime_cpu", "host", {"CpuRealtimeRuntime": 1000}),
    ("blkio_devices", "host", {"BlkioDeviceReadBps": [{"Path": "/dev/x", "Rate": 1}]}),
    ("storage_opt", "host", {"StorageOpt": {"size": "1G"}}),
    ("annotations", "host", {"Annotations": {"a": "b"}}),
    ("isolation", "host", {"Isolation": "hyperv"}),
    ("endpoint_driver_opts", "endpoint", {"DriverOpts": {"x": "y"}}),
    ("unknown_option", "host", {"FutureOption": "on"}),
]

PATCHES = {
    "host": lambda box, values: box.host_config.update(values),
    "endpoint": lambda box, values: box.endpoints[NETWORK].update(values),
}


@pytest.mark.parametrize(
    ("option", "section", "values"),
    [pytest.param(*case, id=case[0]) for case in BODY_OPTIONS],
)
async def test_a_standard_users_recreate_is_refused_each_restricted_option_the_container_carries(
    rig: ContainerRig,
    host: ContainerEngine,
    owned: Box,
    option: str,
    section: str,
    values: dict[str, Any],
) -> None:
    PATCHES[section](owned, values)

    with pytest.raises(RestrictedOptions) as refused:
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert refused.value.refused == (option,)
    assert refused.value.state is None
    assert all(key.startswith(("HostConfig", "NetworkingConfig")) for key in refused.value.fields)
    assert host.verbs() == []
    assert OLD in host.boxes and host.boxes[OLD].state == "running"
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["reason"], row["detail"]["refused"]) == (
        "failure",
        "restricted_option",
        option,
    )
    assert len(rig.locks) == 0


def test_every_option_a_container_can_carry_is_covered_by_the_cases_above() -> None:
    covered = {option for option, _, _ in BODY_OPTIONS}

    assert set(OPTION_IDS) - covered == {
        "host_backed_volume",
        "container_id_file",
        "swarm_volume_placement",
    }


async def test_an_administrators_recreate_of_a_privileged_container_succeeds_and_records_it(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box
) -> None:
    present(recreatable.host_config).update({"Privileged": True, "CapAdd": ["NET_ADMIN"]})

    outcome = await rig.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

    assert present(host.boxes[outcome.new_id].host_config)["Privileged"] is True
    [row] = await recreated_rows(rig)
    assert row["result"] == "success"
    assert set(row["detail"]["restricted"].split(",")) == {"privileged", "cap_add"}


async def test_a_standard_users_own_volume_that_is_host_backed_is_refused_despite_ownership(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    host.volume_docs[VOLUME] = ("local", {"o": "bind", "device": "/"})

    with pytest.raises(RestrictedOptions) as refused:
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert refused.value.refused == ("host_backed_volume",)
    assert host.verbs() == []


async def test_the_confirmation_lists_restricted_options_and_offers_no_submit(
    rig: ContainerRig, owned: Box
) -> None:
    present(owned.host_config).update({"Privileged": True, "PidMode": "host"})

    confirmation = await rig.services.recreation.recreate_confirmation(STANDARD, ENV_A, OLD)

    assert [r.option for r in confirmation.restricted] == ["privileged", "pid_namespace"]
    assert confirmation.can_submit is False


async def test_the_confirmation_describes_what_is_kept_and_what_happens(
    rig: ContainerRig, owned: Box
) -> None:
    confirmation = await rig.services.recreation.recreate_confirmation(STANDARD, ENV_A, OLD)

    assert (confirmation.container_id, confirmation.short_id, confirmation.name) == (
        OLD,
        OLD[:12],
        "web",
    )
    assert (confirmation.image, confirmation.state, confirmation.was_running) == (
        IMAGE,
        "running",
        True,
    )
    assert confirmation.named_volumes == (VOLUME,)
    assert (confirmation.anonymous_volume_count, confirmation.networks) == (1, (NETWORK,))
    assert (confirmation.blockers, confirmation.restricted) == ((), ())
    assert (confirmation.pull_available, confirmation.image_defaults_kept) == (True, False)
    assert confirmation.can_submit is True


async def test_the_confirmation_of_a_stopped_container_says_it_will_not_be_started(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, state="exited")

    confirmation = await rig.services.recreation.recreate_confirmation(ADMIN, ENV_A, OLD)

    assert (confirmation.state, confirmation.was_running) == ("exited", False)


async def test_the_confirmation_blocks_a_container_that_cannot_be_recreated(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host, host_config={"AutoRemove": True})

    confirmation = await rig.services.recreation.recreate_confirmation(ADMIN, ENV_A, OLD)

    assert [b.code for b in confirmation.blockers] == ["auto_remove"]
    assert confirmation.can_submit is False


async def test_the_confirmation_notes_when_the_image_the_container_came_from_is_gone(
    rig: ContainerRig, host: ContainerEngine, recreatable: Box
) -> None:
    host.remove_image(recreatable.image_id)

    confirmation = await rig.services.recreation.recreate_confirmation(ADMIN, ENV_A, OLD)

    assert confirmation.image_defaults_kept is True


@pytest.mark.parametrize(
    ("state", "verbs", "started"),
    [
        pytest.param("running", FULL_SEQUENCE, True, id="running"),
        pytest.param("paused", FULL_SEQUENCE, True, id="paused-comes-back-running"),
        pytest.param("exited", ["rename", "create", "remove"], False, id="stopped-stays-stopped"),
    ],
)
async def test_the_new_container_is_started_only_when_the_old_one_was_live(
    rig: ContainerRig, host: ContainerEngine, state: str, verbs: list[str], started: bool
) -> None:
    seed_recreatable(host, state=state)

    outcome = await rig.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

    assert host.verbs() == verbs
    assert outcome.started is started
    assert host.boxes[outcome.new_id].state == ("running" if started else "created")


def arrange_pull_fails(host: ContainerEngine) -> None:
    host.pull_status = 500


def arrange_stop_fails(host: ContainerEngine) -> None:
    host.fail("stop", 500, target=OLD)


def arrange_stop_conflicts(host: ContainerEngine) -> None:
    host.fail("stop", 409, target=OLD)


def arrange_stop_finds_it_gone(host: ContainerEngine) -> None:
    host.fail("stop", 404, target=OLD)


def arrange_stop_times_out(host: ContainerEngine) -> None:
    host.route("POST", f"/containers/{OLD}/stop", Stall())


def arrange_rename_fails(host: ContainerEngine) -> None:
    host.fail("rename", 500)


def arrange_create_fails(host: ContainerEngine) -> None:
    host.fail("create", 500)


def arrange_create_refused(host: ContainerEngine) -> None:
    host.route("POST", "/containers/create", Refuse())


def arrange_create_times_out(host: ContainerEngine) -> None:
    host.route("POST", "/containers/create", Stall())


def arrange_image_vanishes(host: ContainerEngine) -> None:
    host.remove_image(host.images[IMAGE]["Id"])


def arrange_start_fails(host: ContainerEngine) -> None:
    host.fail("start", 500, target=NEW)


def arrange_rollback_fails(host: ContainerEngine) -> None:
    host.fail("start", 500, target=NEW)
    host.fail("rename", 500, skip=1)


def arrange_start_times_out(host: ContainerEngine) -> None:
    host.route("POST", f"/containers/{NEW}/start", Stall())


RESTORED = (True, "web", "running")
UNTOUCHED_PARKED = (True, PARKED, "exited")

FAILURES = [
    pytest.param(
        arrange_pull_fails, True, PullFailed, ["pull"], RESTORED, "pull_failed", {}, id="pull"
    ),
    pytest.param(
        arrange_stop_fails,
        False,
        RecreateFailed,
        ["stop"],
        RESTORED,
        "engine_rejected",
        {},
        id="stop",
    ),
    pytest.param(
        arrange_stop_conflicts,
        False,
        ContainerStateConflict,
        ["stop"],
        RESTORED,
        "conflict",
        {},
        id="stop-conflict",
    ),
    pytest.param(
        arrange_stop_finds_it_gone,
        False,
        ContainerGone,
        ["stop"],
        RESTORED,
        "not_found",
        {},
        id="stop-not-found",
    ),
    pytest.param(
        arrange_stop_times_out,
        False,
        RecreateFailed,
        ["stop"],
        RESTORED,
        "outcome_unknown",
        {},
        id="stop-outcome-unknown",
    ),
    pytest.param(
        arrange_rename_fails,
        False,
        RecreateFailed,
        ["stop", "rename", "start"],
        RESTORED,
        "engine_rejected",
        {"rolled_back": True},
        id="rename",
    ),
    pytest.param(
        arrange_create_fails,
        False,
        RecreateFailed,
        ["stop", "rename", "create", "rename", "start"],
        RESTORED,
        "engine_rejected",
        {"rolled_back": True},
        id="create",
    ),
    pytest.param(
        arrange_create_refused,
        False,
        EnvironmentUnavailable,
        ["stop", "rename", "create", "rename", "start"],
        RESTORED,
        "unreachable",
        {"rolled_back": True},
        id="create-unreachable",
    ),
    pytest.param(
        arrange_image_vanishes,
        False,
        RecreateFailed,
        ["stop", "rename", "create", "rename", "start"],
        RESTORED,
        "image_missing",
        {"rolled_back": True},
        id="create-image-missing",
    ),
    pytest.param(
        arrange_create_times_out,
        False,
        RecreateFailed,
        ["stop", "rename", "create"],
        UNTOUCHED_PARKED,
        "outcome_unknown",
        {"kept_as": PARKED},
        id="create-outcome-unknown",
    ),
    pytest.param(
        arrange_start_fails,
        False,
        RecreateFailed,
        ["stop", "rename", "create", "start", "remove", "rename", "start"],
        RESTORED,
        "engine_rejected",
        {"rolled_back": True},
        id="start",
    ),
    pytest.param(
        arrange_rollback_fails,
        False,
        RecreateFailed,
        ["stop", "rename", "create", "start", "remove", "rename"],
        UNTOUCHED_PARKED,
        "engine_rejected",
        {"rollback_failed": True, "kept_as": PARKED},
        id="rollback-step",
    ),
    pytest.param(
        arrange_start_times_out,
        False,
        RecreateFailed,
        ["stop", "rename", "create", "start"],
        UNTOUCHED_PARKED,
        "outcome_unknown",
        {"kept_as": PARKED},
        id="start-outcome-unknown",
    ),
]


@pytest.mark.parametrize(
    ("arrange", "pull", "error", "verbs", "original", "reason", "detail"), FAILURES
)
async def test_a_failure_at_each_step_restores_the_original_or_says_where_it_was_left(
    rig: ContainerRig,
    host: ContainerEngine,
    owned: Box,
    arrange: Any,
    pull: bool,
    error: type[Exception],
    verbs: list[str],
    original: tuple[bool, str, str],
    reason: str,
    detail: dict[str, Any],
) -> None:
    arrange(host)

    with pytest.raises(error):
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web", pull=pull)

    assert host.verbs() == verbs
    assert state_of_original(host) == original
    [row] = await recreated_rows(rig)
    assert (row["result"], row["target_id"], row["detail"]["reason"]) == ("failure", OLD, reason)
    assert {k: row["detail"].get(k) for k in detail} == detail
    assert ENGINE_TEXT not in str(row)
    assert (await control_of(rig, OLD_KEY)).owner_user_id == STANDARD_ID
    assert len(rig.locks) == 0


async def test_a_failed_start_leaves_only_the_original_on_the_host(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    arrange_start_fails(host)

    with pytest.raises(RecreateFailed) as failed:
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert failed.value.rolled_back is True
    assert set(host.boxes) == {OLD}
    assert await control_of(rig, NEW_KEY) is None


async def test_a_rollback_that_cannot_finish_names_the_container_that_was_kept(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    arrange_rollback_fails(host)

    with pytest.raises(RecreateFailed) as failed:
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert failed.value.rolled_back is False
    assert PARKED in str(failed.value)


async def test_a_failure_while_the_outcome_is_unknown_is_reported_as_unknown_with_the_kept_name(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    arrange_create_times_out(host)

    with pytest.raises(RecreateFailed) as failed:
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert failed.value.outcome_unknown is True
    assert PARKED in str(failed.value)


async def test_an_old_container_that_cannot_be_removed_is_a_success_that_keeps_it_controlled(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    host.fail("remove", 500, target=OLD)

    outcome = await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert (outcome.old_removed, outcome.kept_as, outcome.started) == (False, PARKED, True)
    assert (outcome.control_moved, outcome.old_control_kept) == (True, True)
    assert host.boxes[OLD].name == PARKED and host.boxes[NEW].name == "web"
    assert (await control_of(rig, OLD_KEY)).owner_user_id == STANDARD_ID
    assert (await control_of(rig, NEW_KEY)).owner_user_id == STANDARD_ID
    [row] = await recreated_rows(rig)
    assert row["result"] == "success"
    assert (row["detail"]["old_removed"], row["detail"]["kept_as"]) == (False, PARKED)
    assert row["detail"]["old_control_kept"] is True


async def test_a_removal_that_times_out_is_a_success_that_says_the_removal_is_unknown(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    host.route("DELETE", f"/containers/{OLD}", Stall())

    outcome = await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert (outcome.old_removed, outcome.remove_outcome_unknown, outcome.kept_as) == (
        False,
        True,
        PARKED,
    )
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["remove_outcome_unknown"]) == ("success", True)


async def test_a_control_that_cannot_be_moved_leaves_the_new_container_administrator_only(
    rig: ContainerRig, owned: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: ResourceControlService, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("database is busy")

    monkeypatch.setattr(ResourceControlService, "transfer", broken)

    outcome = await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert (outcome.control_moved, outcome.old_control_kept) == (False, False)
    restricted = await control_of(rig, NEW_KEY)
    assert (restricted.is_administrators_only, restricted.owner_user_id) == (True, None)
    [row] = await recreated_rows(rig)
    assert (row["detail"]["control_moved"], row["detail"]["control_restricted"]) == (False, True)


async def test_the_old_and_new_containers_are_both_locked_while_the_sequence_runs(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    gate = host.hold("start")
    other = LockHolder(UserActor(ADMIN_ID), "someone", "stop")
    task = asyncio.ensure_future(
        rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")
    )
    await gate.entered.wait()

    with pytest.raises(ObjectBusy) as old_busy:
        async with rig.locks.hold(ObjectKey(ENV_A, "container", OLD), other):
            pass
    with pytest.raises(ObjectBusy) as new_busy:
        async with rig.locks.hold(ObjectKey(ENV_A, "container", NEW), other):
            pass
    gate.release.set()
    await task

    assert old_busy.value.holder.action == new_busy.value.holder.action == "recreate"
    assert len(rig.locks) == 0


async def test_a_container_busy_with_another_action_is_refused_and_recorded_with_the_holder(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    other = LockHolder(UserActor(ADMIN_ID), "someone", "restart")

    async with rig.locks.hold(ObjectKey(ENV_A, "container", OLD), other):
        with pytest.raises(ObjectBusy):
            await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert host.verbs() == []
    [row] = await recreated_rows(rig)
    assert (row["detail"]["reason"], row["detail"]["holder_action"]) == ("object_busy", "restart")
    assert "someone" not in str(row)


async def test_a_cancel_mid_recreate_lets_the_sequence_finish_and_records_the_interruption(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    gate = host.hold("stop")
    task = asyncio.ensure_future(
        rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.verbs() == FULL_SEQUENCE
    [row] = await recreated_rows(rig)
    assert (row["result"], row["target_id"], row["detail"]["interrupted"]) == ("success", OLD, True)
    assert row["detail"]["new_container_id"] == NEW
    assert (await control_of(rig, NEW_KEY)).owner_user_id == STANDARD_ID
    assert await control_of(rig, OLD_KEY) is None
    assert len(rig.locks) == 0


async def test_a_cancel_during_a_pull_before_anything_changed_records_an_interrupted_failure(
    rig: ContainerRig, host: ContainerEngine, owned: Box
) -> None:
    gate = host.hold("pull")
    task = asyncio.ensure_future(
        rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web", pull=True)
    )
    await gate.entered.wait()

    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert host.verbs() == ["pull"]
    assert host.boxes[OLD].state == "running"
    [row] = await recreated_rows(rig)
    assert (row["result"], row["detail"]["reason"]) == ("failure", "interrupted")


async def test_an_anonymous_volume_declared_by_a_mount_entry_keeps_its_data_across_a_recreate(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    scratch = {"Mounts": [{"Type": "volume", "Target": "/scratch"}]}
    old = seed_recreatable(host, host_config=scratch)
    held = next(m["Name"] for m in old.mounts if m["Destination"] == "/scratch")

    outcome = await rig.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

    carried = {m["Destination"]: m["Name"] for m in host.boxes[outcome.new_id].mounts}
    assert carried["/scratch"] == held


SIDECAR = container_id(0xB2)
PROVIDER = container_id(0xB3)


async def test_a_container_sharing_another_network_namespace_is_recreated_without_a_hostname(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_image(IMAGE)
    host.add_configured(PROVIDER, "provider", {"Image": IMAGE})
    sidecar = host.add_configured(
        SIDECAR, "sidecar", {"Image": IMAGE, "HostConfig": {"NetworkMode": f"container:{PROVIDER}"}}
    )
    present(sidecar.config)["Hostname"] = "inherited"

    outcome = await rig.services.recreation.recreate(ADMIN, ENV_A, SIDECAR, expected_name="sidecar")

    assert outcome.started and outcome.old_removed
    assert present(host.boxes[outcome.new_id].host_config)["NetworkMode"] == f"container:{PROVIDER}"


async def test_a_container_whose_description_is_over_the_read_limit_cannot_be_recreated(
    rig: ContainerRig, host: ContainerEngine, owned: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "CONTAINER_INSPECT_BYTES", 10)

    with pytest.raises(ContainerTooLarge):
        await rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")

    assert host.verbs() == []
    assert await recreated_rows(rig) == []


async def test_a_recreate_that_outlives_the_wait_after_a_cancel_is_settled_when_it_finishes(
    rig: ContainerRig, host: ContainerEngine, owned: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(container_provisioning, "SETTLE_WAIT_SECONDS", SHORT_WAIT)
    gate = host.hold("stop")
    task = asyncio.ensure_future(
        rig.services.recreation.recreate(STANDARD, ENV_A, OLD, expected_name="web")
    )
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unknown = await recreated_rows(rig)

    gate.release.set()
    await other_tasks()
    await other_tasks()

    [first] = unknown
    assert (first["result"], first["detail"]["reason"]) == ("failure", "outcome_unknown")
    settled = (await recreated_rows(rig))[1]
    assert (settled["result"], settled["target_id"]) == ("success", OLD)
    assert settled["detail"]["interrupted"] is True and settled["detail"]["settled_late"] is True
    assert (await control_of(rig, NEW_KEY)).owner_user_id == STANDARD_ID
    assert host.verbs() == FULL_SEQUENCE
