"""Standard-user restrictions and the reach rule on service create, update and rollback."""

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.errors import ValidationFailed
from app.services.access import ResourceKey
from app.services.object_inspector import SwarmUnavailable
from app.services.swarm_service_views import RestrictedService, ServiceInvalid
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, control
from tests.docker_engine.container_engine import (
    HOST_NETWORK_ID,
    swarm_network_id,
)
from tests.docker_engine.swarm_engine import MANAGER, plain_spec
from tests.object_support import rows_of
from tests.services.conftest import SwarmRig
from tests.swarm_support import NAMESPACE_LABEL

FORM = {"name": "web", "image": "busybox:latest", "mode": "replicated", "replicas": "1"}
PIN = f"node.id=={MANAGER}"
OVERLAY = swarm_network_id(5)
PLACEHOLDER = swarm_network_id(6)


def with_form(**fields: str) -> dict[str, str]:
    return {**FORM, **fields}


async def own(rig: SwarmRig, **fields: str) -> str:
    created = await rig.sw.service_actions.create(STANDARD, ENV_A, with_form(**fields))
    rig.fresh()
    return created.service_id


def plant(rig: SwarmRig, service_id: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    """Change the stored spec behind the product's back and move its version."""
    mutate(rig.host.services[service_id]["Spec"])
    rig.host.services[service_id]["Version"]["Index"] += 1
    rig.fresh()


def container(spec: dict[str, Any]) -> dict[str, Any]:
    returned: dict[str, Any] = spec["TaskTemplate"]["ContainerSpec"]
    return returned


def set_key(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(spec: dict[str, Any]) -> None:
        node = spec
        for step in path[:-1]:
            node = node.setdefault(step, {})
        node[path[-1]] = value

    return mutate


CARRIED: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("cap_add", set_key(("TaskTemplate", "ContainerSpec", "CapabilityAdd"), ["SYS_ADMIN"])),
    (
        "security_opt",
        set_key(
            ("TaskTemplate", "ContainerSpec", "Privileges"), {"Seccomp": {"Mode": "unconfined"}}
        ),
    ),
    ("sysctls", set_key(("TaskTemplate", "ContainerSpec", "Sysctls"), {"net.x": "1"})),
    ("isolation", set_key(("TaskTemplate", "ContainerSpec", "Isolation"), "hyperv")),
    ("oom_score_adj", set_key(("TaskTemplate", "ContainerSpec", "OomScoreAdj"), -500)),
    (
        "bind_mount",
        set_key(
            ("TaskTemplate", "ContainerSpec", "Mounts"),
            [{"Type": "bind", "Source": "/", "Target": "/host"}],
        ),
    ),
    (
        "volume_driver",
        set_key(
            ("TaskTemplate", "ContainerSpec", "Mounts"),
            [
                {
                    "Type": "volume",
                    "Source": "dd",
                    "Target": "/d",
                    "VolumeOptions": {"DriverConfig": {"Name": "nfs"}},
                }
            ],
        ),
    ),
    ("log_driver", set_key(("TaskTemplate", "LogDriver"), {"Name": "syslog"})),
    ("runtime", set_key(("TaskTemplate", "Runtime"), "plugin")),
    (
        "devices",
        set_key(
            ("TaskTemplate", "Resources", "Reservations"),
            {"GenericResources": [{"DiscreteResourceSpec": {"Kind": "gpu", "Value": 1}}]},
        ),
    ),
    (
        "endpoint_driver_opts",
        set_key(("TaskTemplate", "Networks"), [{"Target": OVERLAY, "DriverOpts": {"a": "b"}}]),
    ),
    ("unknown_option", set_key(("Zzz",), {"x": 1})),
    ("unknown_option", set_key(("TaskTemplate", "ContainerSpec", "privileged"), True)),
]


@pytest.mark.parametrize(
    ("option", "mutate"), [pytest.param(o, m, id=f"{o}-{i}") for i, (o, m) in enumerate(CARRIED)]
)
async def test_a_standard_users_update_is_refused_for_a_restricted_option_the_stored_spec_carries(
    swarm: SwarmRig, option: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    swarm.host.add_network("ov", 0, "overlay", network_id=OVERLAY, Scope="swarm")
    service_id = await own(swarm)
    plant(swarm, service_id, mutate)
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)
    before = len(swarm.host.mutating())

    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.update(
            STANDARD,
            ENV_A,
            service_id,
            {**offer.values, "command": "a b"},
            expected_version=offer.version,
        )

    row = (await rows_of(swarm.db, "service_updated"))[-1]
    assert option in raised.value.refused
    assert (row["result"], row["detail"]["reason"]) == ("failure", "restricted")
    assert option in row["detail"]["refused"]
    assert len(swarm.host.mutating()) == before


@pytest.mark.parametrize("option", ["cap_add", "bind_mount", "security_opt"])
async def test_the_same_update_by_an_administrator_goes_through_and_records_what_was_restricted(
    swarm: SwarmRig, option: str
) -> None:
    service_id = await own(swarm)
    plant(swarm, service_id, dict(CARRIED)[option])
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)

    await swarm.sw.service_actions.update(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "a b"},
        expected_version=offer.version,
    )

    row = (await rows_of(swarm.db, "service_updated"))[-1]
    assert row["result"] == "success" and option in row["detail"]["restricted"]


@pytest.mark.parametrize(
    ("fields", "option", "field"),
    [
        (
            {"mount-0-type": "bind", "mount-0-source": "/etc", "mount-0-target": "/x"},
            "bind_mount",
            "mount-0-type",
        ),
    ],
)
async def test_the_form_cannot_create_what_a_standard_user_may_not_have(
    swarm: SwarmRig, fields: dict[str, str], option: str, field: str
) -> None:
    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, with_form(**fields))

    row = (await rows_of(swarm.db, "service_created"))[-1]
    assert option in raised.value.refused and field in raised.value.fields
    assert row["detail"]["reason"] == "restricted" and swarm.host.services == {}


async def test_an_administrator_may_bind_mount_and_the_row_records_the_restriction(
    swarm: SwarmRig,
) -> None:
    fields = {"mount-0-type": "bind", "mount-0-source": "/etc", "mount-0-target": "/x"}

    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, with_form(**fields))

    [row] = await rows_of(swarm.db, "service_created")
    assert created.service_id in swarm.host.services
    assert row["detail"]["restricted"] == "bind_mount"


async def test_a_standard_users_service_is_theirs_alone(swarm: SwarmRig) -> None:
    created = await swarm.sw.service_actions.create(STANDARD, ENV_A, FORM)
    swarm.fresh()

    mine = await swarm.sw.services.page(STANDARD, ENV_A)
    theirs = await swarm.sw.services.page(PEER, ENV_A)
    view = await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "service", created.service_id))

    assert created.control_created and [r.name for r in mine.page.items] == ["web"]
    assert theirs.page.items == [] and view is not None and view.owner_user_id == STANDARD.user_id


@pytest.mark.parametrize("label", [NAMESPACE_LABEL, "COM.docker.compose.project"])
async def test_a_reserved_label_is_refused_for_everyone(swarm: SwarmRig, label: str) -> None:
    fields = {"label-0-key": label, "label-0-value": "x"}

    for who in (STANDARD, ADMIN):
        with pytest.raises(ValidationFailed) as raised:
            await swarm.sw.service_actions.create(who, ENV_A, with_form(**fields))
        assert "label-0-key" in raised.value.fields

    assert swarm.host.services == {}


async def test_a_standard_user_may_run_at_most_100_replicas_and_an_administrator_1000(
    swarm: SwarmRig,
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, with_form(replicas="101"))
    created = await swarm.sw.service_actions.create(
        ADMIN, ENV_A, with_form(name="big", replicas="1000")
    )

    assert "replicas" in raised.value.fields
    assert swarm.host.services[created.service_id]["Spec"]["Mode"] == {
        "Replicated": {"Replicas": 1000}
    }


async def test_a_network_resolves_by_name_or_id_to_its_id_in_the_sent_spec(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_network("ov", 0, "overlay", network_id=OVERLAY, Scope="swarm", Attachable=True)

    by_name = await swarm.sw.service_actions.create(
        ADMIN, ENV_A, with_form(name="a", **{"network-0-name": "ov"})
    )
    by_id = await swarm.sw.service_actions.create(
        ADMIN, ENV_A, with_form(name="b", **{"network-0-name": OVERLAY})
    )

    for created in (by_name, by_id):
        spec = swarm.host.services[created.service_id]["Spec"]
        assert spec["TaskTemplate"]["Networks"] == [{"Target": OVERLAY}]


async def test_a_network_that_does_not_exist_and_one_the_user_cannot_write_read_the_same(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_network("ov", 0, "overlay", network_id=OVERLAY, Scope="swarm")

    with pytest.raises(ServiceInvalid) as missing:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, with_form(**{"network-0-name": "nosuch"})
        )
    with pytest.raises(ServiceInvalid) as denied:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, with_form(**{"network-0-name": "ov"})
        )

    assert missing.value.fields == denied.value.fields
    assert list(missing.value.fields) == ["network-0-name"]


async def test_write_access_to_a_network_lets_a_standard_user_join_it(swarm: SwarmRig) -> None:
    swarm.host.add_network("ov", 0, "overlay", network_id=OVERLAY, Scope="swarm")
    await control(
        swarm.access,
        ResourceKey(ENV_A, "network", OVERLAY),
        level="write",
        users=(STANDARD.user_id,),
    )
    swarm.fresh()

    created = await swarm.sw.service_actions.create(
        STANDARD, ENV_A, with_form(**{"network-0-name": "ov"})
    )

    spec = swarm.host.services[created.service_id]["Spec"]
    assert spec["TaskTemplate"]["Networks"] == [{"Target": OVERLAY}]


@pytest.mark.parametrize(
    ("spelling", "refusal", "refused"),
    [
        pytest.param("host", ServiceInvalid, (), id="by name"),
        pytest.param(PLACEHOLDER, RestrictedService, ("network_host",), id="swarm placeholder id"),
        pytest.param(HOST_NETWORK_ID, ServiceInvalid, (), id="local host id"),
    ],
)
async def test_every_spelling_of_the_host_network_is_refused_for_a_standard_user(
    swarm: SwarmRig, spelling: str, refusal: type[ServiceInvalid], refused: tuple[str, ...]
) -> None:
    swarm.host.add_network("host-placeholder", 0, "host", network_id=PLACEHOLDER, Scope="swarm")
    key = ResourceKey(ENV_A, "network", PLACEHOLDER)
    await control(swarm.access, key, level="write", public=True)
    swarm.fresh()

    with pytest.raises(refusal) as raised:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, with_form(**{"network-0-name": spelling})
        )

    assert "network-0-name" in raised.value.fields and swarm.host.services == {}
    assert getattr(raised.value, "refused", ()) == refused


def attach(kind: str, name: str) -> dict[str, str]:
    return {f"{kind}-0-name": name, f"{kind}-0-target": f"/{name}", f"{kind}-0-mode": "0444"}


async def test_a_config_needs_read_access_and_a_secret_needs_write_access_to_attach(
    swarm: SwarmRig,
) -> None:
    config_id = swarm.host.add_config("cfg")
    secret_id = swarm.host.add_secret("tok")
    await control(swarm.access, ResourceKey(ENV_A, "config", config_id), users=(STANDARD.user_id,))
    await control(swarm.access, ResourceKey(ENV_A, "secret", secret_id), users=(STANDARD.user_id,))
    swarm.fresh()

    with pytest.raises(ServiceInvalid) as read_only_secret:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, with_form(**attach("secret", "tok")))
    ok = await swarm.sw.service_actions.create(
        STANDARD, ENV_A, with_form(name="cfgonly", **attach("config", "cfg"))
    )

    assert list(read_only_secret.value.fields) == ["secret-0-name"]
    refs = swarm.host.services[ok.service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]["Configs"]
    assert refs[0]["ConfigID"] == config_id


async def test_write_access_to_a_secret_lets_it_be_attached_by_id_and_name(
    swarm: SwarmRig,
) -> None:
    secret_id = swarm.host.add_secret("tok")
    await control(
        swarm.access,
        ResourceKey(ENV_A, "secret", secret_id),
        level="write",
        users=(STANDARD.user_id,),
    )
    swarm.fresh()

    created = await swarm.sw.service_actions.create(
        STANDARD, ENV_A, with_form(**attach("secret", "tok"))
    )

    refs = container(swarm.host.services[created.service_id]["Spec"])["Secrets"]
    assert (refs[0]["SecretID"], refs[0]["SecretName"]) == (secret_id, "tok")


async def test_an_unreadable_config_and_a_missing_one_read_the_same(swarm: SwarmRig) -> None:
    swarm.host.add_config("hidden")

    with pytest.raises(ServiceInvalid) as denied:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, with_form(**attach("config", "hidden"))
        )
    with pytest.raises(ServiceInvalid) as missing:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, with_form(**attach("config", "nosuch"))
        )

    assert denied.value.fields == missing.value.fields


async def test_references_a_service_already_holds_are_not_rechecked_on_update(
    swarm: SwarmRig,
) -> None:
    secret_id = swarm.host.add_secret("tok")
    service_id = swarm.host.named_service("held", secrets=[(secret_id, "tok")])
    key = ResourceKey(ENV_A, "service", service_id)
    await control(swarm.access, key, level="write", users=(STANDARD.user_id,))
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)

    await swarm.sw.service_actions.update(
        STANDARD,
        ENV_A,
        service_id,
        {**offer.values, "command": "a b"},
        expected_version=offer.version,
    )

    kept = container(swarm.host.services[service_id]["Spec"])["Secrets"]
    assert kept[0]["SecretID"] == secret_id


async def test_a_new_secret_cannot_be_added_to_a_service_by_someone_who_cannot_write_it(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_secret("tok")
    service_id = await own(swarm)
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)

    with pytest.raises(ServiceInvalid) as raised:
        await swarm.sw.service_actions.update(
            STANDARD,
            ENV_A,
            service_id,
            {**offer.values, **attach("secret", "tok")},
            expected_version=offer.version,
        )

    assert list(raised.value.fields) == ["secret-0-name"]


VOLUME = {"mount-0-type": "volume", "mount-0-source": "data", "mount-0-target": "/d"}


async def test_a_named_volume_without_the_manager_pin_is_refused_with_the_placement_id(
    swarm: SwarmRig,
) -> None:
    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, with_form(**VOLUME))

    assert raised.value.refused == ("swarm_volume_placement",)
    assert "mount-0-source" in raised.value.fields
    assert swarm.host.services == {}


async def test_a_named_volume_is_allowed_once_the_service_is_pinned_to_the_manager(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.service_actions.create(
        STANDARD, ENV_A, with_form(constraints=PIN, **VOLUME)
    )

    [row] = await rows_of(swarm.db, "service_created")
    assert created.service_id in swarm.host.services
    assert row["detail"]["pinned_to_manager"] is True


async def test_run_on_manager_adds_the_pin_the_form_needs(swarm: SwarmRig) -> None:
    created = await swarm.sw.service_actions.create(
        STANDARD, ENV_A, with_form(run_on_manager="on", **VOLUME)
    )

    placement = swarm.host.services[created.service_id]["Spec"]["TaskTemplate"]["Placement"]
    assert placement["Constraints"] == [PIN]


async def test_anonymous_volumes_and_tmpfs_need_no_pin(swarm: SwarmRig) -> None:
    fields = {
        "mount-0-type": "volume",
        "mount-0-target": "/anon",
        "mount-1-type": "tmpfs",
        "mount-1-target": "/tmp",
    }

    created = await swarm.sw.service_actions.create(STANDARD, ENV_A, with_form(**fields))

    assert created.service_id in swarm.host.services


async def test_an_existing_volume_needs_write_access_and_a_host_backed_one_is_refused(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_volume("data")
    swarm.host.add_volume("hostbacked", options={"type": "none", "o": "bind", "device": "/"})
    pinned = with_form(constraints=PIN)

    with pytest.raises(ServiceInvalid) as denied:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, {**pinned, **VOLUME})
    await control(
        swarm.access, ResourceKey(ENV_A, "volume", "hostbacked"), level="write", public=True
    )
    swarm.fresh()
    with pytest.raises(RestrictedService) as backed:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, {**pinned, **VOLUME, "mount-0-source": "hostbacked"}
        )

    assert list(denied.value.fields) == ["mount-0-source"]
    assert backed.value.refused == ("host_backed_volume",)


async def test_an_unchanged_named_mount_with_unchanged_constraints_stays_updatable(
    swarm: SwarmRig,
) -> None:
    service_id = await own(swarm, constraints="node.role==worker")
    plant(
        swarm,
        service_id,
        set_key(
            ("TaskTemplate", "ContainerSpec", "Mounts"),
            [{"Type": "volume", "Source": "data", "Target": "/d"}],
        ),
    )
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)

    await swarm.sw.service_actions.update(
        STANDARD,
        ENV_A,
        service_id,
        {**offer.values, "command": "a b"},
        expected_version=offer.version,
    )
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)
    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.update(
            STANDARD,
            ENV_A,
            service_id,
            {**offer.values, "constraints": "node.role==manager"},
            expected_version=offer.version,
        )

    assert raised.value.refused == ("swarm_volume_placement",)


def previous_with(mutate: Callable[[dict[str, Any]], None]) -> Callable[[SwarmRig, str], None]:
    def apply(rig: SwarmRig, service_id: str) -> None:
        previous = plain_spec("web")
        mutate(previous)
        rig.host.services[service_id]["PreviousSpec"] = previous
        rig.host.services[service_id]["Version"]["Index"] += 1
        rig.fresh()

    return apply


async def test_a_standard_users_rollback_to_a_spec_with_a_bind_mount_is_refused_and_sent_nowhere(
    swarm: SwarmRig,
) -> None:
    service_id = await own(swarm)
    previous_with(
        set_key(
            ("TaskTemplate", "ContainerSpec", "Mounts"),
            [{"Type": "bind", "Source": "/", "Target": "/h"}],
        )
    )(swarm, service_id)
    version = (await swarm.sw.services.detail(STANDARD, ENV_A, service_id)).version
    swarm.fresh()
    before = len(swarm.host.mutating())

    with pytest.raises(RestrictedService) as impact:
        await swarm.sw.services.rollback_impact(STANDARD, ENV_A, service_id)
    with pytest.raises(RestrictedService):
        await swarm.sw.service_actions.rollback(
            STANDARD, ENV_A, service_id, expected_version=version
        )

    row = (await rows_of(swarm.db, "service_rolled_back"))[-1]
    assert "bind_mount" in impact.value.refused and row["detail"]["reason"] == "restricted"
    assert len(swarm.host.mutating()) == before


async def test_an_administrators_rollback_to_the_same_spec_is_allowed_and_recorded(
    swarm: SwarmRig,
) -> None:
    service_id = await own(swarm)
    previous_with(
        set_key(
            ("TaskTemplate", "ContainerSpec", "Mounts"),
            [{"Type": "bind", "Source": "/", "Target": "/h"}],
        )
    )(swarm, service_id)
    version = (await swarm.sw.services.detail(ADMIN, ENV_A, service_id)).version
    swarm.fresh()

    await swarm.sw.service_actions.rollback(ADMIN, ENV_A, service_id, expected_version=version)

    row = (await rows_of(swarm.db, "service_rolled_back"))[-1]
    assert row["result"] == "success" and "bind_mount" in row["detail"]["restricted"]


async def test_a_config_lookup_that_fails_is_an_outage_not_a_field_error(
    swarm: SwarmRig,
) -> None:
    swarm.host.add_config("app-cfg")
    swarm.host.outage = {"configs"}

    with pytest.raises(SwarmUnavailable) as raised:
        await swarm.sw.service_actions.create(
            ADMIN, ENV_A, with_form(**attach("config", "app-cfg"))
        )
    swarm.host.outage = set()
    created = await swarm.sw.service_actions.create(
        ADMIN, ENV_A, with_form(**attach("config", "app-cfg"))
    )

    [failed, ok] = await rows_of(swarm.db, "service_created")
    assert raised.value.kind == "not_swarm_manager"
    assert (failed["result"], failed["detail"]["reason"]) == ("failure", "not_swarm_manager")
    assert ok["result"] == "success" and created.service_id in swarm.host.services
