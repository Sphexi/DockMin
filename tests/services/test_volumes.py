"""The volume query and action services over a fake daemon and the seeded database."""

import asyncio
from typing import Any

import pytest

import app.services.volumes as volumes_module
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.assembly import VolumeServices
from app.services.container_actions import EngineRefused
from app.services.object_inspector import EnvironmentUnavailable
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.resource_controls import ResourceControlService
from app.services.volume_views import (
    ForceRefused,
    RestrictedVolume,
    VolumeChangedSinceReview,
    VolumeFilters,
    VolumeGone,
    VolumeInUse,
    VolumeNameInUse,
    VolumePruneTarget,
    VolumeRefused,
    containers_digest,
)
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    PEER,
    STANDARD,
    container_id,
    control,
    stack_key,
    volume_key,
)
from tests.docker_engine.container_engine import (
    ANONYMOUS_LABEL,
    ENGINE_TEXT,
    ContainerEngine,
    anonymous_volume,
)
from tests.docker_engine.fake_engine import Stall
from tests.object_support import (
    STACK_LABEL,
    SWARM_LABEL,
    add_container,
    drain_background,
    rows_of,
    volume_mount,
)
from tests.services.conftest import ContainerRig

CREATED_AT = "2024-05-06T07:08:09Z"
OTHER_CREATED_AT = "2024-06-01T00:00:00Z"
HEX_NAME = "2" * 64
SMALL_SETTLE = 0.05
MAX_PRUNE = 100
MAX_FORCE = 25
NAME_LIMIT = 20


def names(page: Any) -> list[str]:
    return [row.name for row in page.page.items]


def force_queries(host: ContainerEngine) -> list[dict[str, str]]:
    return [query for _, query in host.volume_removals()]


def holder(user: int = PEER.user_id) -> LockHolder:
    return LockHolder(UserActor(user), "peer", "remove")


@pytest.fixture
def catalogue(host: ContainerEngine) -> None:
    """A stack volume, a plugin volume, a used volume and a daemon-made anonymous one."""
    host.add_volume("data", labels={STACK_LABEL: "Shop"})
    host.add_volume("nfs-data", driver="nfs", options={"o": "addr=1"})
    host.add_volume("used-vol")
    host.add_volume(anonymous_volume(1), anonymous=True)
    add_container(host, 1, "user", mounts=[volume_mount("used-vol")])


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (VolumeFilters(), {"data", "nfs-data", "used-vol", anonymous_volume(1)}),
        (VolumeFilters(q="  "), {"data", "nfs-data", "used-vol", anonymous_volume(1)}),
        (VolumeFilters(q="DAT"), {"data", "nfs-data"}),
        (VolumeFilters(driver="other"), {"nfs-data"}),
        (VolumeFilters(driver="local"), {"data", "used-vol", anonymous_volume(1)}),
        (VolumeFilters(usage="used"), {"used-vol"}),
        (VolumeFilters(usage="unused"), {"data", "nfs-data", anonymous_volume(1)}),
        (VolumeFilters(kind="anonymous"), {anonymous_volume(1)}),
        (VolumeFilters(kind="named"), {"data", "nfs-data", "used-vol"}),
        (VolumeFilters(stack="SHOP"), {"data"}),
        (VolumeFilters(stack="shop", driver="local", usage="unused", kind="named"), {"data"}),
    ],
)
async def test_filters_narrow_the_decoded_list_by_name_driver_use_kind_and_stack(
    vols: VolumeServices,
    catalogue: None,
    filters: VolumeFilters,
    expected: set[str],
) -> None:
    page = await vols.queries.page(ADMIN, ENV_A, filters)

    assert set(names(page)) == expected and page.total == len(expected)


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        (VolumeFilters(driver="nfs"), "driver"),
        (VolumeFilters(usage="sometimes"), "usage"),
        (VolumeFilters(kind="mine"), "kind"),
        (VolumeFilters(q="x" * 129), "q"),
        (VolumeFilters(stack="x" * 129), "stack"),
    ],
)
async def test_an_invalid_filter_is_refused_on_its_own_field_before_the_host_is_read(
    vols: VolumeServices, host: ContainerEngine, filters: VolumeFilters, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await vols.queries.page(ADMIN, ENV_A, filters)

    assert field in refused.value.fields and host.requests == []


async def test_the_list_orders_by_casefolded_name_and_pages_with_a_cursor(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    for name in ("Bravo", "alpha", "Charlie", "delta", "echo"):
        host.add_volume(name)

    first = await vols.queries.page(ADMIN, ENV_A, limit=2)
    second = await vols.queries.page(ADMIN, ENV_A, limit=2, cursor=first.page.next_cursor)
    third = await vols.queries.page(ADMIN, ENV_A, limit=2, cursor=second.page.next_cursor)

    assert names(first) + names(second) + names(third) == [
        "alpha",
        "Bravo",
        "Charlie",
        "delta",
        "echo",
    ]
    assert third.page.next_cursor is None and first.total == 5


async def test_an_anonymous_volume_is_listed_by_a_shortened_name(
    vols: VolumeServices, catalogue: None
) -> None:
    page = await vols.queries.page(ADMIN, ENV_A, VolumeFilters(kind="anonymous"))

    (row,) = page.page.items
    assert (row.short_name, row.kind) == (anonymous_volume(1)[:12], "anonymous")


async def test_a_volume_named_like_an_anonymous_id_is_anonymous_without_the_label(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume(HEX_NAME)

    (row,) = (await vols.queries.page(ADMIN, ENV_A)).page.items

    assert row.kind == "anonymous"


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize(
    "name",
    ["missing", "..", "a" * 256, "f" * 64, "-", ".hidden", "a/b", "data ", "", "nul\x00"],
)
async def test_a_missing_or_malformed_volume_name_is_not_found_for_every_role(
    vols: VolumeServices, host: ContainerEngine, actor: Any, name: str
) -> None:
    with pytest.raises(ResourceNotFound):
        await vols.queries.detail(actor, ENV_A, name)
    with pytest.raises(ResourceNotFound):
        await vols.queries.remove_impact(actor, ENV_A, name)
    with pytest.raises(ResourceNotFound):
        await vols.queries.force_impact(actor, ENV_A, name)


async def test_a_hidden_volume_and_a_missing_one_are_the_same_not_found_for_a_standard_user(
    vols: VolumeServices, catalogue: None
) -> None:
    with pytest.raises(ResourceNotFound) as hidden:
        await vols.queries.detail(STANDARD, ENV_A, "data")
    with pytest.raises(ResourceNotFound) as missing:
        await vols.queries.detail(STANDARD, ENV_A, "missing")

    assert str(hidden.value) == str(missing.value)


async def test_a_standard_user_sees_the_volumes_a_grant_or_a_stack_reaches_and_no_others(
    rig: ContainerRig, vols: VolumeServices, catalogue: None
) -> None:
    await control(rig.access, volume_key("nfs-data"), public=True)
    await control(rig.access, stack_key("shop"), public=True)

    page = await vols.queries.page(STANDARD, ENV_A)

    assert set(names(page)) == {"data", "nfs-data"}
    assert (page.visible, page.total) == (2, 2)


@pytest.mark.parametrize(
    ("labels", "visible"),
    [
        ({STACK_LABEL: "shop"}, True),
        ({STACK_LABEL: "ShOp"}, True),
        ({SWARM_LABEL: "SHOP"}, True),
        ({"COM.DOCKER.COMPOSE.PROJECT": "shop"}, False),
        ({STACK_LABEL: "other"}, False),
        ({}, False),
    ],
)
async def test_a_volume_is_visible_through_the_stack_label_in_any_case_of_its_value_only(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    labels: dict[str, str],
    visible: bool,
) -> None:
    host.add_volume("probe", labels=labels)
    await control(rig.access, ResourceKey(ENV_A, "stack", "shop"), public=True)

    page = await vols.queries.page(STANDARD, ENV_A)

    assert ("probe" in names(page)) is visible


async def test_a_volumes_own_control_wins_over_the_stack_it_belongs_to(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("inherited", labels={STACK_LABEL: "shop"})
    host.add_volume("overridden", labels={STACK_LABEL: "shop"})
    await control(rig.access, ResourceKey(ENV_A, "stack", "shop"), public=True)
    await control(rig.access, volume_key("overridden"), administrators_only=True)

    page = await vols.queries.page(STANDARD, ENV_A)

    assert names(page) == ["inherited"]
    with pytest.raises(ResourceNotFound):
        await vols.queries.detail(STANDARD, ENV_A, "overridden")


async def test_a_standard_user_on_a_local_socket_reads_but_cannot_create_or_remove(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("local-vol")
    await control(rig.access, volume_key("local-vol", ENV_B), public=True, level="write")

    page = await vols.queries.page(STANDARD, ENV_B)

    assert names(page) == ["local-vol"] and not page.can_create
    assert not any(row.can_remove for row in page.page.items)
    assert await vols.queries.names(STANDARD, ENV_B, "local", limit=NAME_LIMIT) == [
        ("local-vol", False)
    ]
    with pytest.raises(PermissionDenied):
        await vols.queries.require_create(STANDARD, ENV_B)
    with pytest.raises(PermissionDenied):
        await vols.actions.create(STANDARD, ENV_B, {"name": "nope"})
    with pytest.raises(PermissionDenied):
        await vols.actions.remove(
            STANDARD, ENV_B, "local-vol", expected_created_at=CREATED_AT, expected_in_use=0
        )
    assert host.volume_bodies() == [] and host.volume_removals() == []
    assert await rows_of(rig.db, "volume_created") == []


async def test_the_detail_withholds_the_host_path_and_options_from_a_standard_user(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume(
        "share",
        driver="local",
        options={"type": "cifs", "o": "user=svc%Hunter2Pass", "device": "//10.0.0.5/s"},
        labels={"api_key": "k-1", "team": "core"},
    )
    await control(rig.access, volume_key("share"), public=True)

    admin = await vols.queries.detail(ADMIN, ENV_A, "share")
    standard = await vols.queries.detail(STANDARD, ENV_A, "share")

    assert admin.mountpoint == "/var/lib/docker/volumes/share/_data"
    assert {(o.key, o.redacted) for o in admin.options or ()} == {
        ("type", False),
        ("o", True),
        ("device", False),
    }
    assert (standard.mountpoint, standard.options) == (None, None)
    assert [(v.key, v.redacted) for v in standard.labels] == [("api_key", True), ("team", False)]
    assert "Hunter2Pass" not in repr(admin) + repr(standard)


async def test_the_detail_cuts_the_container_names_at_ten_and_counts_the_cut_apart_from_the_hidden(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), public=True)
    for number in range(1, 15):
        add_container(host, number, f"c{number:02d}", mounts=[volume_mount("shared")])

    admin = await vols.queries.detail(ADMIN, ENV_A, "shared")
    standard = await vols.queries.detail(STANDARD, ENV_A, "shared")

    assert (len(admin.containers), admin.containers_more, admin.containers_hidden) == (10, 4, 0)
    assert admin.containers_total == 14
    assert (standard.containers, standard.containers_more, standard.containers_hidden) == (
        (),
        0,
        14,
    )


async def test_a_long_label_value_is_clipped_in_the_detail(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("noted", labels={"note": "x" * 10_000})

    detail = await vols.queries.detail(ADMIN, ENV_A, "noted")

    assert len(detail.labels[0].value) == 4097


async def test_names_mark_write_access_and_match_the_named_volumes_the_list_shows(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("rw")
    host.add_volume("ro")
    host.add_volume("bound", options={"o": "bind", "device": "/etc", "type": "none"})
    host.add_volume("plugin", driver="nfs")
    host.add_volume("hidden")
    host.add_volume(anonymous_volume(1), anonymous=True)
    await control(rig.access, volume_key("rw"), public=True, level="write")
    await control(rig.access, volume_key("ro"), public=True, level="read")
    await control(rig.access, volume_key("bound"), public=True, level="write")
    await control(rig.access, volume_key("plugin"), public=True, level="write")

    standard = await vols.queries.names(STANDARD, ENV_A, "", limit=NAME_LIMIT)
    admin = await vols.queries.names(ADMIN, ENV_A, "", limit=NAME_LIMIT)
    listed = names(await vols.queries.page(STANDARD, ENV_A))

    assert standard == [("bound", True), ("plugin", True), ("ro", False), ("rw", True)]
    assert [name for name, _ in standard] == sorted(listed)
    assert admin == [
        ("bound", True),
        ("hidden", True),
        ("plugin", True),
        ("ro", True),
        ("rw", True),
    ]


async def test_names_keep_only_the_prefix_ignoring_case_and_stop_at_the_limit(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("Web-b")
    host.add_volume("web-a")
    host.add_volume("db")
    for number in range(30):
        host.add_volume(f"v{number:02d}")

    prefixed = await vols.queries.names(ADMIN, ENV_A, "WEB", limit=NAME_LIMIT)
    limited = await vols.queries.names(ADMIN, ENV_A, "v", limit=NAME_LIMIT)

    assert [name for name, _ in prefixed] == ["web-a", "Web-b"]
    assert [name for name, _ in limited] == [f"v{number:02d}" for number in range(NAME_LIMIT)]


async def test_names_read_the_host_volume_list_once(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("web")

    await vols.queries.names(ADMIN, ENV_A, "w", limit=NAME_LIMIT)

    assert [p for p in host.paths() if p.startswith("GET /volumes")] == ["GET /volumes"]


async def test_an_administrator_creates_a_volume_with_options_and_the_row_names_option_keys_only(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    form = {
        "name": "share",
        "driver": "local",
        "option-0-key": "type",
        "option-0-value": "cifs",
        "option-1-key": "o",
        "option-1-value": "addr=1.2.3.4,password=hunter2",
        "label-0-key": "team",
        "label-0-value": "core",
    }

    created = await vols.actions.create(ADMIN, ENV_A, form)

    assert (created.control_created, created.preexisting) == (True, False)
    assert host.volume_bodies() == [
        {
            "Name": "share",
            "Driver": "local",
            "DriverOpts": {"type": "cifs", "o": "addr=1.2.3.4,password=hunter2"},
            "Labels": {"team": "core"},
        }
    ]
    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["result"], row["target_type"], row["target_id"]) == ("success", "volume", "share")
    assert row["detail"] == {
        "driver": "local",
        "option_keys": "type,o",
        "labels": 1,
        "restricted": "options",
        "control_created": True,
    }
    assert "hunter2" not in str(row)
    owned = await rig.access.controls.get(ADMIN, volume_key("share"))
    assert owned is not None and owned.owner_user_id == ADMIN.user_id


async def test_an_administrator_creating_a_plugin_volume_records_the_driver_as_restricted(
    rig: ContainerRig, vols: VolumeServices
) -> None:
    await vols.actions.create(ADMIN, ENV_A, {"name": "remote", "driver": "vieux/sshfs:latest"})

    (row,) = await rows_of(rig.db, "volume_created")
    assert row["detail"]["restricted"] == "driver"


async def test_a_standard_user_creates_a_plain_local_volume_and_owns_it(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    created = await vols.actions.create(STANDARD, ENV_A, {"name": "mine", "driver": "local"})

    owned = await rig.access.controls.get(ADMIN, volume_key("mine"))
    assert created.control_created and owned is not None
    assert owned.owner_user_id == STANDARD.user_id
    assert names(await vols.queries.page(STANDARD, ENV_A)) == ["mine"]
    (row,) = await rows_of(rig.db, "volume_created")
    assert "restricted" not in row["detail"]


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"driver": "nfs"}, "driver"),
        ({"driver": "LOCAL"}, "driver"),
        ({"driver": "local-persist"}, "driver"),
        ({"option-0-key": "o", "option-0-value": "bind"}, "option-0-key"),
        ({"option-0-key": "type", "option-0-value": "none"}, "option-0-key"),
        ({"option-0-key": "device", "option-0-value": "/etc"}, "option-0-key"),
        ({"option-0-key": "size", "option-0-value": "1G"}, "option-0-key"),
    ],
)
async def test_a_standard_user_is_refused_any_driver_but_local_and_any_driver_option(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    extra: dict[str, str],
    field: str,
) -> None:
    with pytest.raises(RestrictedVolume) as refused:
        await vols.actions.create(STANDARD, ENV_A, {"name": "vx"} | extra)

    assert field in refused.value.fields
    assert host.volume_bodies() == [] and "vx" not in host.volumes
    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "restricted")


@pytest.mark.parametrize("key", ["o", "device", "size"])
async def test_a_refused_driver_option_is_named_by_its_key_in_the_message_and_the_row(
    rig: ContainerRig, vols: VolumeServices, key: str
) -> None:
    form = {"name": "vx", "option-0-key": key, "option-0-value": "v"}

    with pytest.raises(RestrictedVolume) as refused:
        await vols.actions.create(STANDARD, ENV_A, form)

    (row,) = await rows_of(rig.db, "volume_created")
    assert refused.value.fields["option-0-key"] == f"Only administrators can set the option {key}."
    assert row["detail"]["option_keys"] == key


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize(
    "key",
    [STACK_LABEL, SWARM_LABEL, STACK_LABEL.upper(), f" {STACK_LABEL} ", SWARM_LABEL.title()],
)
async def test_a_volume_cannot_be_created_carrying_an_inheritance_label_in_any_spelling(
    vols: VolumeServices, host: ContainerEngine, actor: Any, key: str
) -> None:
    form = {"name": "lab", "label-0-key": key, "label-0-value": "shop"}

    with pytest.raises(ValidationFailed) as refused:
        await vols.actions.create(actor, ENV_A, form)

    assert "labels" in refused.value.fields and host.volume_bodies() == []


@pytest.mark.parametrize("actor", [ADMIN, STANDARD])
@pytest.mark.parametrize(
    "key",
    [ANONYMOUS_LABEL, ANONYMOUS_LABEL.upper(), f" {ANONYMOUS_LABEL} ", ANONYMOUS_LABEL.title()],
)
async def test_a_volume_cannot_be_created_carrying_the_anonymous_marker_label(
    vols: VolumeServices, host: ContainerEngine, actor: Any, key: str
) -> None:
    form = {"name": "lbl", "label-0-key": key, "label-0-value": ""}

    with pytest.raises(ValidationFailed) as refused:
        await vols.actions.create(actor, ENV_A, form)

    assert "label-0-key" in refused.value.fields and "lbl" not in host.volumes


@pytest.mark.parametrize(
    "name",
    ["", "a", "-x", "..", "a/b", "a b", "a" * 256, HEX_NAME],
)
async def test_a_name_outside_the_grammar_or_shaped_like_an_anonymous_id_is_refused(
    vols: VolumeServices, host: ContainerEngine, name: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await vols.actions.create(ADMIN, ENV_A, {"name": name})

    assert "name" in refused.value.fields and host.volume_bodies() == []


async def test_a_name_that_already_exists_is_refused_not_adopted(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("someone")

    with pytest.raises(VolumeNameInUse):
        await vols.actions.create(STANDARD, ENV_A, {"name": "someone"})

    assert host.volume_bodies() == []
    assert await rig.access.controls.get(ADMIN, volume_key("someone")) is None
    (row,) = await rows_of(rig.db, "volume_created")
    assert row["detail"]["reason"] == "name_in_use"


async def test_a_volume_that_appears_in_the_moment_before_the_create_is_not_claimed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.on("volume_create", lambda: host.add_volume("raced", labels={"owner": "someone"}))

    created = await vols.actions.create(STANDARD, ENV_A, {"name": "raced"})

    assert (created.preexisting, created.control_created) == (True, False)
    assert await rig.access.controls.get(ADMIN, volume_key("raced")) is None
    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["detail"]["preexisting"], row["detail"]["control_created"]) == (True, False)


async def test_an_identical_plain_volume_that_appears_in_that_moment_is_claimed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.on("volume_create", lambda: host.add_volume("twin"))

    created = await vols.actions.create(STANDARD, ENV_A, {"name": "twin"})

    assert (created.preexisting, created.control_created) == (False, True)


async def test_a_control_left_on_the_name_by_an_earlier_volume_is_replaced_by_the_new_owners(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    await control(rig.access, volume_key("data"), level="write", owner=PEER.user_id)

    created = await vols.actions.create(STANDARD, ENV_A, {"name": "data"})

    assert created.control_created
    owned = await rig.access.controls.get(ADMIN, volume_key("data"))
    assert owned is not None and owned.owner_user_id == STANDARD.user_id
    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["detail"]["stale_control_replaced"], row["detail"]["control_created"]) == (
        True,
        True,
    )


async def test_a_create_is_refused_while_another_request_holds_the_name(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    async with rig.engine.locks.hold(ObjectKey(ENV_A, "volume", "busy"), holder()):
        with pytest.raises(ObjectBusy):
            await vols.actions.create(ADMIN, ENV_A, {"name": "busy"})

    assert host.volume_bodies() == []
    (row,) = await rows_of(rig.db, "volume_created")
    assert row["detail"]["reason"] == "object_busy"


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [(500, VolumeRefused, "engine_rejected"), (400, VolumeRefused, "engine_rejected")],
)
async def test_an_engine_refusal_of_a_create_is_a_product_error_with_no_engine_text(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    status: int,
    error: type[Exception],
    reason: str,
) -> None:
    host.fail("volume_create", status)

    with pytest.raises(error) as refused:
        await vols.actions.create(ADMIN, ENV_A, {"name": "nope"})

    assert ENGINE_TEXT not in str(refused.value)
    (row,) = await rows_of(rig.db, "volume_created")
    assert row["detail"]["reason"] == reason and ENGINE_TEXT not in str(row)


async def test_a_create_that_times_out_is_an_unknown_outcome(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.route("POST", "/volumes/create", Stall())

    with pytest.raises(EnvironmentUnavailable):
        await vols.actions.create(ADMIN, ENV_A, {"name": "slow"})

    (row,) = await rows_of(rig.db, "volume_created")
    assert row["detail"]["reason"] == "outcome_unknown"


async def test_a_failed_claim_leaves_the_volume_in_place_uncontrolled_and_says_so(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing(self: ResourceControlService, key: ResourceKey, owner: int | None) -> bool:
        raise RuntimeError("claim failed")

    monkeypatch.setattr(ResourceControlService, "claim", failing)

    created = await vols.actions.create(STANDARD, ENV_A, {"name": "mine"})

    assert not created.control_created and "mine" in host.volumes
    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["result"], row["detail"]["control_created"]) == ("success", False)


async def test_a_create_cancelled_while_the_engine_works_still_claims_and_records_the_volume(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    gate = host.hold("volume_create")
    task = asyncio.ensure_future(vols.actions.create(STANDARD, ENV_A, {"name": "slow"}))
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await rows_of(rig.db, "volume_created")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)
    owned = await rig.access.controls.get(ADMIN, volume_key("slow"))
    assert owned is not None and owned.owner_user_id == STANDARD.user_id


async def test_a_create_that_outlives_the_wait_is_recorded_unknown_and_settled_late(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(volumes_module, "SETTLE_SECONDS", SMALL_SETTLE)
    gate = host.hold("volume_create")
    task = asyncio.ensure_future(vols.actions.create(STANDARD, ENV_A, {"name": "slow"}))
    await gate.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release.set()
    await drain_background()

    first, second = await rows_of(rig.db, "volume_created")
    assert (first["result"], first["detail"]["reason"]) == ("failure", "outcome_unknown")
    assert (second["result"], second["detail"]["settled_late"]) == ("success", True)
    assert await rig.access.controls.get(ADMIN, volume_key("slow")) is not None


async def test_a_volume_is_removed_after_review_and_its_control_is_released(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    await vols.actions.create(STANDARD, ENV_A, {"name": "mine"})
    impact = await vols.queries.remove_impact(STANDARD, ENV_A, "mine")

    removed = await vols.actions.remove(
        STANDARD, ENV_A, "mine", expected_created_at=impact.created_at_text, expected_in_use=0
    )

    assert removed.control_released and "mine" not in host.volumes
    assert await rig.access.controls.get(ADMIN, volume_key("mine")) is None
    (row,) = await rows_of(rig.db, "volume_removed")
    assert row["detail"] == {
        "driver": "local",
        "force": False,
        "containers_removed": 0,
        "in_use_containers": 0,
        "control_released": True,
    }
    assert all("true" not in query.values() for query in force_queries(host))


async def test_the_product_never_sends_force_to_the_engine_on_any_removal_path(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("plain")
    host.add_volume("shared")
    add_container(host, 1, "user", mounts=[volume_mount("shared")])
    impact = await vols.queries.force_impact(ADMIN, ENV_A, "shared")
    await vols.actions.remove(
        ADMIN, ENV_A, "plain", expected_created_at=CREATED_AT, expected_in_use=0
    )

    await vols.actions.remove_with_containers(
        ADMIN,
        ENV_A,
        "shared",
        expected_created_at=CREATED_AT,
        expected_containers=impact.containers_digest,
        expected_names={c.id: c.name for c in impact.containers},
    )

    assert [name for name, _ in host.volume_removals()] == ["plain", "shared"]
    assert all("true" not in query.values() for query in force_queries(host))


@pytest.mark.parametrize("mount_count", [1, 2])
async def test_a_volume_mounted_by_a_stopped_container_is_refused_even_when_the_review_agreed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, mount_count: int
) -> None:
    host.add_volume("shared")
    for number in range(1, mount_count + 1):
        add_container(host, number, f"c{number}", mounts=[volume_mount("shared")])

    with pytest.raises(VolumeInUse) as refused:
        await vols.actions.remove(
            ADMIN, ENV_A, "shared", expected_created_at=CREATED_AT, expected_in_use=mount_count
        )

    assert refused.value.containers == mount_count and host.volume_removals() == []
    (row,) = await rows_of(rig.db, "volume_removed")
    assert row["detail"]["reason"] == "volume_in_use"


@pytest.mark.parametrize(
    ("created_at", "in_use"), [(OTHER_CREATED_AT, 0), (CREATED_AT, 1), (CREATED_AT, 2)]
)
async def test_a_volume_that_changed_since_the_review_is_not_removed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, created_at: str, in_use: int
) -> None:
    host.add_volume("shared")

    with pytest.raises(VolumeChangedSinceReview):
        await vols.actions.remove(
            ADMIN, ENV_A, "shared", expected_created_at=created_at, expected_in_use=in_use
        )

    assert host.volume_removals() == []
    (row,) = await rows_of(rig.db, "volume_removed")
    assert row["detail"]["reason"] == "changed_since_review"


async def test_an_engine_not_found_on_the_delete_releases_the_stale_control_and_says_so(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    await vols.actions.create(STANDARD, ENV_A, {"name": "gone"})
    host.fail("volume_remove", 404)

    with pytest.raises(VolumeGone):
        await vols.actions.remove(
            STANDARD, ENV_A, "gone", expected_created_at=CREATED_AT, expected_in_use=0
        )

    assert await rig.access.controls.get(ADMIN, volume_key("gone")) is None
    (row,) = await rows_of(rig.db, "volume_removed")
    assert (row["detail"]["reason"], row["detail"]["control_released"]) == ("volume_gone", True)


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [(409, VolumeInUse, "volume_in_use"), (500, EngineRefused, "engine_rejected")],
)
async def test_an_engine_refusal_of_a_remove_is_a_product_error_with_no_engine_text(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    status: int,
    error: type[Exception],
    reason: str,
) -> None:
    host.add_volume("plain")
    host.fail("volume_remove", status)

    with pytest.raises(error) as refused:
        await vols.actions.remove(
            ADMIN, ENV_A, "plain", expected_created_at=CREATED_AT, expected_in_use=0
        )

    assert ENGINE_TEXT not in str(refused.value)
    (row,) = await rows_of(rig.db, "volume_removed")
    assert row["detail"]["reason"] == reason and ENGINE_TEXT not in str(row)


async def test_a_remove_is_refused_while_another_request_holds_the_volume(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("locked")

    async with rig.engine.locks.hold(ObjectKey(ENV_A, "volume", "locked"), holder()):
        with pytest.raises(ObjectBusy):
            await vols.actions.remove(
                ADMIN, ENV_A, "locked", expected_created_at=CREATED_AT, expected_in_use=0
            )

    assert host.volume_removals() == []


async def test_a_failed_release_still_counts_the_removal_and_the_row_says_so(
    rig: ContainerRig,
    vols: VolumeServices,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await vols.actions.create(ADMIN, ENV_A, {"name": "mine"})

    async def failing(self: ResourceControlService, key: ResourceKey) -> bool:
        raise RuntimeError("release failed")

    monkeypatch.setattr(ResourceControlService, "release", failing)

    removed = await vols.actions.remove(
        ADMIN, ENV_A, "mine", expected_created_at=CREATED_AT, expected_in_use=0
    )

    assert not removed.control_released and "mine" not in host.volumes
    (row,) = await rows_of(rig.db, "volume_removed")
    assert (row["result"], row["detail"]["control_released"]) == ("success", False)


async def test_a_remove_cancelled_while_the_engine_works_is_recorded_as_removed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("slow")
    gate = host.hold("volume_remove")
    task = asyncio.ensure_future(
        vols.actions.remove(ADMIN, ENV_A, "slow", expected_created_at=CREATED_AT, expected_in_use=0)
    )
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    (row,) = await rows_of(rig.db, "volume_removed")
    assert (row["result"], row["detail"]["interrupted"]) == ("success", True)


async def test_a_standard_user_removes_only_a_volume_they_hold_write_on(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("admins")
    host.add_volume("readable")
    await control(rig.access, volume_key("readable"), public=True, level="read")

    with pytest.raises(ResourceNotFound):
        await vols.actions.remove(
            STANDARD, ENV_A, "admins", expected_created_at=CREATED_AT, expected_in_use=0
        )
    with pytest.raises(PermissionDenied):
        await vols.actions.remove(
            STANDARD, ENV_A, "readable", expected_created_at=CREATED_AT, expected_in_use=0
        )

    assert host.volume_removals() == [] and await rows_of(rig.db, "volume_removed") == []


@pytest.fixture
def shared(host: ContainerEngine) -> list[str]:
    """A volume mounted by two stopped containers, returned as the container ids."""
    host.add_volume("shared")
    add_container(host, 1, "c-one", mounts=[volume_mount("shared")])
    add_container(host, 2, "c-two", mounts=[volume_mount("shared", "/d2")])
    return [container_id(1), container_id(2)]


async def force(vols: VolumeServices, actor: Any = ADMIN, **overrides: Any) -> Any:
    impact = await vols.queries.force_impact(actor, ENV_A, "shared")
    arguments: dict[str, Any] = {
        "expected_created_at": impact.created_at_text,
        "expected_containers": impact.containers_digest,
        "expected_names": {c.id: c.name for c in impact.containers},
    }
    return await vols.actions.remove_with_containers(
        actor, ENV_A, "shared", **(arguments | overrides)
    )


async def test_the_force_impact_names_every_container_with_its_state_and_the_digest_binding_them(
    vols: VolumeServices, shared: list[str]
) -> None:
    impact = await vols.queries.force_impact(ADMIN, ENV_A, "shared")

    assert [(c.name, c.state) for c in impact.containers] == [
        ("c-one", "exited"),
        ("c-two", "exited"),
    ]
    assert impact.containers_digest == containers_digest(shared) and not impact.blocked
    assert (impact.running, impact.unwritable, impact.too_many) == ((), 0, False)


async def test_a_force_removal_removes_the_containers_then_the_volume_and_records_each_step(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, shared: list[str]
) -> None:
    result = await force(vols)

    assert result.failure is None and result.containers_removed == 2
    assert [s.status for s in result.steps] == ["removed", "removed"]
    assert host.boxes == {} and "shared" not in host.volumes
    container_rows = await rows_of(rig.db, "container_removed")
    assert [r["target_id"] for r in container_rows] == shared
    (row,) = await rows_of(rig.db, "volume_removed")
    assert (row["detail"]["force"], row["detail"]["containers_removed"]) == (True, 2)
    removals = [
        r for r in host.requests if r.method == "DELETE" and r.path.startswith("/containers/")
    ]
    assert all("true" not in r.query.values() for r in removals)


async def test_a_force_removal_refuses_everything_when_a_container_is_running(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, shared: list[str]
) -> None:
    impact = await vols.queries.force_impact(ADMIN, ENV_A, "shared")
    host.boxes[shared[1]].state = "running"

    with pytest.raises(ForceRefused) as refused:
        await vols.actions.remove_with_containers(
            ADMIN,
            ENV_A,
            "shared",
            expected_created_at=impact.created_at_text,
            expected_containers=impact.containers_digest,
            expected_names={c.id: c.name for c in impact.containers},
        )

    assert [c.name for c in refused.value.running] == ["c-two"]
    assert len(host.boxes) == 2 and "shared" in host.volumes


@pytest.mark.parametrize("state", ["running", "paused", "restarting"])
async def test_the_force_impact_marks_each_live_state_as_blocking(
    vols: VolumeServices, host: ContainerEngine, shared: list[str], state: str
) -> None:
    host.boxes[shared[0]].state = state

    impact = await vols.queries.force_impact(ADMIN, ENV_A, "shared")

    assert impact.blocked and [c.name for c in impact.running] == ["c-one"]


@pytest.mark.parametrize(
    "override",
    [
        {"expected_created_at": OTHER_CREATED_AT},
        {"expected_containers": "0" * 64},
        {"expected_names": {}},
        {"expected_names": {container_id(1): "renamed", container_id(2): "c-two"}},
    ],
)
async def test_a_force_removal_whose_review_no_longer_matches_changes_nothing(
    vols: VolumeServices, host: ContainerEngine, shared: list[str], override: dict[str, Any]
) -> None:
    with pytest.raises(VolumeChangedSinceReview):
        await force(vols, **override)

    assert len(host.boxes) == 2 and host.volume_removals() == []


async def test_a_container_added_after_the_review_changes_the_digest_and_blocks_the_force(
    vols: VolumeServices, host: ContainerEngine, shared: list[str]
) -> None:
    impact = await vols.queries.force_impact(ADMIN, ENV_A, "shared")
    add_container(host, 3, "late", mounts=[volume_mount("shared", "/d3")])

    with pytest.raises(VolumeChangedSinceReview):
        await vols.actions.remove_with_containers(
            ADMIN,
            ENV_A,
            "shared",
            expected_created_at=impact.created_at_text,
            expected_containers=impact.containers_digest,
            expected_names={c.id: c.name for c in impact.containers},
        )

    assert len(host.boxes) == 3


async def test_the_force_run_stops_at_the_first_container_that_is_not_removed(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, shared: list[str]
) -> None:
    host.fail("remove", 500, target=shared[0])

    result = await force(vols)

    assert result.volume is None and result.failure is not None
    assert [s.status for s in result.steps] == ["failed"]
    assert len(host.boxes) == 2 and "shared" in host.volumes
    assert await rows_of(rig.db, "volume_removed") == []


async def test_more_than_twenty_five_users_refuse_the_force_and_the_impact_says_so(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("crowd")
    for number in range(1, MAX_FORCE + 2):
        add_container(host, number, f"c{number:02d}", mounts=[volume_mount("crowd")])

    impact = await vols.queries.force_impact(ADMIN, ENV_A, "crowd")

    assert impact.too_many and impact.blocked
    assert (len(impact.containers), impact.containers_more) == (MAX_FORCE, 1)
    with pytest.raises(ForceRefused) as refused:
        await vols.actions.remove_with_containers(
            ADMIN,
            ENV_A,
            "crowd",
            expected_created_at=impact.created_at_text,
            expected_containers=impact.containers_digest,
            expected_names={c.id: c.name for c in impact.containers},
        )
    assert refused.value.too_many and len(host.boxes) == MAX_FORCE + 1


async def test_a_standard_user_forces_their_own_volume_and_containers(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    await vols.actions.create(STANDARD, ENV_A, {"name": "shared"})
    add_container(host, 1, "mine", mounts=[volume_mount("shared")])
    await control(
        rig.access, ResourceKey(ENV_A, "container", container_id(1)), public=True, level="write"
    )

    result = await force(vols, STANDARD)

    assert result.volume is not None and host.boxes == {}


async def test_a_standard_user_cannot_force_a_volume_used_by_a_container_they_cannot_change(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    await vols.actions.create(STANDARD, ENV_A, {"name": "shared"})
    add_container(host, 1, "mine", mounts=[volume_mount("shared")])
    add_container(host, 2, "theirs", mounts=[volume_mount("shared", "/d2")])
    await control(
        rig.access, ResourceKey(ENV_A, "container", container_id(1)), public=True, level="write"
    )

    impact = await vols.queries.force_impact(STANDARD, ENV_A, "shared")
    with pytest.raises(ForceRefused) as refused:
        await vols.actions.remove_with_containers(
            STANDARD,
            ENV_A,
            "shared",
            expected_created_at=impact.created_at_text,
            expected_containers=impact.containers_digest,
            expected_names={c.id: c.name for c in impact.containers},
        )

    assert (impact.unwritable, impact.containers_hidden) == (1, 1)
    assert [c.name for c in impact.containers] == ["mine"] and "theirs" not in repr(impact)
    assert refused.value.unwritable == 1 and len(host.boxes) == 2


@pytest.fixture
def prunable(host: ContainerEngine) -> dict[str, str]:
    """A daemon-made anonymous volume, a labelled named one, a hex-named one, and more."""
    host.add_volume(anonymous_volume(1), anonymous=True, created=CREATED_AT)
    host.add_volume("labelled-name", anonymous=True)
    host.add_volume(HEX_NAME)
    host.add_volume("named-unused")
    host.add_volume("used")
    add_container(host, 1, "user", mounts=[volume_mount("used")])
    return {"anonymous": anonymous_volume(1)}


async def test_the_anonymous_prune_lists_only_unused_volumes_with_the_label_and_a_hex_name(
    vols: VolumeServices, prunable: dict[str, str]
) -> None:
    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "anonymous")

    assert [c.name for c in plan.candidates] == [prunable["anonymous"]]


async def test_the_all_mode_prune_lists_every_unused_volume_named_ones_included(
    vols: VolumeServices, prunable: dict[str, str]
) -> None:
    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "all")

    assert [c.name for c in plan.candidates] == sorted(
        [HEX_NAME, "labelled-name", "named-unused", prunable["anonymous"]], key=str.casefold
    )
    assert not plan.truncated


async def test_a_prune_mode_that_is_not_offered_is_refused(vols: VolumeServices) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await vols.queries.prune_plan(ADMIN, ENV_A, "everything")  # type: ignore[arg-type]

    assert "mode" in refused.value.fields


async def test_the_prune_plan_is_cut_at_one_hundred_and_says_how_many_matched(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    for number in range(MAX_PRUNE + 5):
        host.add_volume(f"spare{number:03d}")

    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "all")

    assert (len(plan.candidates), plan.total, plan.truncated) == (MAX_PRUNE, MAX_PRUNE + 5, True)


async def test_a_prune_removes_exactly_the_reviewed_volumes_that_are_still_candidates(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine, prunable: dict[str, str]
) -> None:
    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "all")
    reviewed = [VolumePruneTarget(c.name, c.created_at_text) for c in plan.candidates]
    add_container(host, 2, "late", mounts=[volume_mount("named-unused")])
    stale = [VolumePruneTarget("used", CREATED_AT), VolumePruneTarget(HEX_NAME, OTHER_CREATED_AT)]
    targets = [t for t in reviewed if t.name != HEX_NAME] + stale

    result = await vols.actions.run_prune(ADMIN, ENV_A, "all", targets)

    assert {i.name: i.status for i in result.items} == {
        prunable["anonymous"]: "removed",
        "labelled-name": "removed",
        "named-unused": "skipped_changed",
        "used": "skipped_changed",
        HEX_NAME: "skipped_changed",
    }
    assert {"named-unused", "used", HEX_NAME} <= set(host.volumes)
    pruned = [r for r in await rows_of(rig.db, "volume_removed") if r["detail"].get("pruned")]
    assert {r["target_id"] for r in pruned} == {prunable["anonymous"], "labelled-name"}
    assert all("true" not in query.values() for query in force_queries(host))


async def test_the_anonymous_mode_does_not_remove_a_named_volume_listed_in_a_forged_selection(
    vols: VolumeServices, host: ContainerEngine, prunable: dict[str, str]
) -> None:
    result = await vols.actions.run_prune(
        ADMIN, ENV_A, "anonymous", [VolumePruneTarget("named-unused", CREATED_AT)]
    )

    assert [i.status for i in result.items] == ["skipped_changed"]
    assert "named-unused" in host.volumes


async def test_a_busy_volume_is_skipped_by_prune_not_waited_for(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("busy")
    host.add_volume("free")
    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "all")

    async with rig.engine.locks.hold(ObjectKey(ENV_A, "volume", "busy"), holder()):
        result = await vols.actions.run_prune(
            ADMIN,
            ENV_A,
            "all",
            [VolumePruneTarget(c.name, c.created_at_text) for c in plan.candidates],
        )

    assert {i.name: i.status for i in result.items} == {"busy": "busy", "free": "removed"}


async def test_a_prune_item_the_engine_refuses_does_not_stop_the_rest(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("first")
    host.add_volume("second")
    host.fail("volume_remove", 500, times=1)
    plan = await vols.queries.prune_plan(ADMIN, ENV_A, "all")

    result = await vols.actions.run_prune(
        ADMIN, ENV_A, "all", [VolumePruneTarget(c.name, c.created_at_text) for c in plan.candidates]
    )

    assert [i.status for i in result.items] == ["failed", "removed"]


@pytest.mark.parametrize(
    "targets", [[], [VolumePruneTarget("../x", CREATED_AT)], [VolumePruneTarget("", CREATED_AT)]]
)
async def test_a_prune_with_no_valid_selection_is_refused(
    vols: VolumeServices, targets: list[VolumePruneTarget]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await vols.actions.run_prune(ADMIN, ENV_A, "all", targets)

    assert "volumes" in refused.value.fields


async def test_a_prune_of_more_than_one_hundred_volumes_is_refused(vols: VolumeServices) -> None:
    targets = [VolumePruneTarget(f"v{n:03d}", CREATED_AT) for n in range(MAX_PRUNE + 1)]

    with pytest.raises(ValidationFailed):
        await vols.actions.run_prune(ADMIN, ENV_A, "all", targets)


async def test_prune_is_for_administrators_only(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    with pytest.raises(PermissionDenied):
        await vols.queries.prune_plan(STANDARD, ENV_A, "all")
    with pytest.raises(PermissionDenied):
        await vols.actions.run_prune(STANDARD, ENV_A, "all", [VolumePruneTarget("x1", CREATED_AT)])

    assert host.volume_removals() == []


async def test_the_access_tab_is_for_administrators_only(
    rig: ContainerRig, vols: VolumeServices, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), public=True)

    panel = await vols.queries.access_panel(ADMIN, ENV_A, "shared")

    assert panel.name == "shared" and panel.control is not None
    with pytest.raises(PermissionDenied):
        await vols.queries.access_panel(STANDARD, ENV_A, "shared")


async def test_a_volume_whose_name_cannot_be_keyed_is_left_out_of_the_list(
    vols: VolumeServices, host: ContainerEngine
) -> None:
    for name in ("fine", "bad name!", "-leading", "a" * 256):
        host.add_volume(name)

    page = await vols.queries.page(ADMIN, ENV_A)

    assert names(page) == ["fine"]
