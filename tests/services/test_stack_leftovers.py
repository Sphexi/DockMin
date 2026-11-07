"""Leftover-only projects and namespaces: tokens, digests, applicability and what the lists show."""

import hashlib

import pytest

from app.db.repositories.stack_dismissal import StoredDismissal
from app.docker_engine.models import (
    ContainerSummary,
    Network,
    SwarmConfig,
    SwarmSecret,
    SwarmService,
    Volume,
)
from app.services.object_inspector import SWARM_OBJECT_ID
from app.services.stack_dismissal_views import (
    DISMISSIBLE_NAME,
    MAX_LEFTOVERS_SHOWN,
    HostMembers,
    LeftoverObject,
    LeftoverVolume,
    applies,
    compose_members,
    config_token,
    dismissal_view,
    leftover_objects,
    leftover_volumes,
    members_digest,
    network_token,
    secret_token,
    swarm_members,
    volume_token,
)
from tests.access_support import ADMIN, ENV_A
from tests.docker_engine.container_engine import VOLUME_CREATED, swarm_network_id
from tests.docker_engine.swarm_engine import plain_spec
from tests.stack_support import PROJECT_LABEL, Rig, project_box
from tests.swarm_stack_support import StackDaemon, swarm_services
from tests.swarm_support import NAMESPACE_LABEL

SEEN_AT = "2026-01-02T03:04:05Z"
NETWORK_A = "network:a"
VOLUME_V = "volume:v:t"
STORED = StoredDismissal("shop", (NETWORK_A, VOLUME_V), None, "pat", SEEN_AT)
MAX_VOLUME_NAME = 255
LONG_NAME = "v" * 200
HOSTILE_NAME = "evil/<b>x</b>"
LABEL_CUT = 128
EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ONE_DIGEST = "04cec16c0f501b476a33283290e006f87de0c107c5cd1beb26b4e6ec98a0a83b"
TWO_DIGEST = "31d22e85d6b5d23060f3fda4ecbc6d483dbd56325c19bad736c59587ef87aae9"


def volume(name: str, created_at: str = "t") -> Volume:
    return Volume.model_validate({"Name": name, "CreatedAt": created_at})


def network(object_id: str, name: str = "net") -> Network:
    return Network.model_validate({"Id": object_id, "Name": name})


def config(object_id: str, name: str = "cfg") -> SwarmConfig:
    return SwarmConfig.model_validate({"ID": object_id, "Version": {"Index": 1}, "Name": name})


def secret(object_id: str, name: str = "sec") -> SwarmSecret:
    return SwarmSecret.model_validate({"ID": object_id, "Version": {"Index": 1}, "Name": name})


def container(object_id: str) -> ContainerSummary:
    return ContainerSummary.model_validate(
        {"Id": object_id, "Image": "i", "Created": 0, "State": "exited"}
    )


def service(object_id: str) -> SwarmService:
    return SwarmService.model_validate({"ID": object_id, "Version": {"Index": 1}})


def test_each_token_names_the_kind_and_the_identity_of_its_object() -> None:
    tokens = (
        volume_token(volume("data", "2024-05-06T07:08:09Z")),
        network_token(network("n1")),
        config_token(config("c1")),
        secret_token(secret("s1")),
    )

    assert tokens == ("volume:data:2024-05-06T07:08:09Z", "network:n1", "config:c1", "secret:s1")


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        pytest.param([], EMPTY_DIGEST, id="nothing"),
        pytest.param([NETWORK_A], ONE_DIGEST, id="one"),
        pytest.param([NETWORK_A, VOLUME_V], TWO_DIGEST, id="two-in-order"),
        pytest.param([VOLUME_V, NETWORK_A], TWO_DIGEST, id="two-out-of-order"),
    ],
)
def test_the_members_digest_is_the_sha256_of_the_sorted_ids_joined_by_newlines(
    ids: list[str], expected: str
) -> None:
    assert members_digest(ids) == expected


def test_a_different_object_set_gives_a_different_digest() -> None:
    assert members_digest([NETWORK_A]) != members_digest([NETWORK_A, VOLUME_V])
    assert members_digest([NETWORK_A]) == hashlib.sha256(NETWORK_A.encode()).hexdigest()


def test_compose_members_count_containers_and_hold_sorted_unique_tokens() -> None:
    held = compose_members(
        [container("c1"), container("c2")],
        [volume("data", "t")],
        [network("n2"), network("n1"), network("n1")],
    )

    assert held.workloads == 2
    assert held.tokens == ("network:n1", "network:n2", "volume:data:t")


def test_swarm_members_count_services_and_hold_sorted_unique_tokens() -> None:
    held = swarm_members(
        [service("s1")], [config("c1")], [secret("x1")], [network("n1"), network("n1")]
    )

    assert held.workloads == 1
    assert held.tokens == ("config:c1", "network:n1", "secret:x1")


@pytest.mark.parametrize(
    ("members", "present"),
    [
        pytest.param(HostMembers(0, ()), False, id="nothing"),
        pytest.param(HostMembers(1, ()), True, id="a-workload"),
        pytest.param(HostMembers(0, (NETWORK_A,)), True, id="an-object"),
    ],
)
def test_an_entry_is_present_when_it_holds_a_workload_or_an_object(
    members: HostMembers, present: bool
) -> None:
    assert members.present is present


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        pytest.param(HostMembers(0, ()), True, id="everything-gone"),
        pytest.param(HostMembers(0, (NETWORK_A,)), True, id="a-subset"),
        pytest.param(HostMembers(0, (NETWORK_A, VOLUME_V)), True, id="the-same-set"),
        pytest.param(HostMembers(0, (NETWORK_A, "config:new")), False, id="a-new-object"),
        pytest.param(HostMembers(0, ("volume:v:recreated",)), False, id="a-recreated-volume"),
        pytest.param(HostMembers(1, (NETWORK_A,)), False, id="a-workload"),
        pytest.param(HostMembers(1, ()), False, id="a-workload-and-no-objects"),
    ],
)
def test_a_dismissal_applies_while_no_workload_and_no_unnamed_object_is_present(
    members: HostMembers, expected: bool
) -> None:
    assert applies(STORED, members) is expected


def test_with_no_stored_dismissal_there_is_no_view() -> None:
    assert dismissal_view(None, HostMembers(0, (NETWORK_A,))) is None


@pytest.mark.parametrize(
    ("members", "changed"),
    [
        pytest.param(HostMembers(0, (NETWORK_A,)), False, id="still-covered"),
        pytest.param(HostMembers(1, (NETWORK_A,)), True, id="a-container-appeared"),
        pytest.param(HostMembers(0, ("network:new",)), True, id="an-object-appeared"),
    ],
)
def test_a_dismissal_view_reports_when_the_entry_changed_since(
    members: HostMembers, changed: bool
) -> None:
    view = dismissal_view(STORED, members)

    assert view is not None
    assert (view.dismissed_at, view.dismissed_by) == (SEEN_AT, "pat")
    assert (view.applies, view.changed_since) == (not changed, changed)


@pytest.mark.parametrize(
    ("name", "linkable"),
    [
        pytest.param("data", True, id="plain"),
        pytest.param("a.b_c-d", True, id="punctuation"),
        pytest.param("x" * MAX_VOLUME_NAME, True, id="longest"),
        pytest.param("x" * (MAX_VOLUME_NAME + 1), False, id="too-long"),
        pytest.param(HOSTILE_NAME, False, id="slash"),
        pytest.param("-lead", False, id="leading-dash"),
        pytest.param("a b", False, id="space"),
        pytest.param("", False, id="empty"),
    ],
)
def test_a_volume_is_linkable_only_when_its_name_is_one_a_page_can_open(
    name: str, linkable: bool
) -> None:
    [shown] = leftover_volumes([volume(name)])

    assert shown.linkable is linkable


def test_a_long_volume_keeps_its_full_name_and_shows_a_cut_label() -> None:
    [shown] = leftover_volumes([volume(LONG_NAME, "t")])

    assert shown == LeftoverVolume(LONG_NAME, LONG_NAME[:LABEL_CUT], "t", True)


def test_the_volumes_shown_are_sorted_by_name_and_cut_at_the_limit() -> None:
    names = [f"v{number:02d}" for number in range(MAX_LEFTOVERS_SHOWN + 5)]

    shown = leftover_volumes([volume(name) for name in reversed(names)])

    assert [item.name for item in shown] == names[:MAX_LEFTOVERS_SHOWN]
    assert len(leftover_volumes([volume(name) for name in names], limit=3)) == 3


@pytest.mark.parametrize(
    ("object_id", "grammar_is_swarm", "linkable"),
    [
        pytest.param("a" * 64, False, True, id="network-64-hex"),
        pytest.param("a" * 25, False, True, id="network-25-chars"),
        pytest.param("a" * 24, False, False, id="network-24-chars"),
        pytest.param("A" * 25, False, False, id="network-upper-case"),
        pytest.param("a" * 25, True, True, id="swarm-25-chars"),
        pytest.param("a" * 64, True, False, id="swarm-64-hex"),
    ],
)
def test_an_object_is_linkable_when_its_id_matches_the_grammar_it_is_checked_against(
    object_id: str, grammar_is_swarm: bool, linkable: bool
) -> None:
    grammar = (SWARM_OBJECT_ID,) if grammar_is_swarm else ()

    [shown] = leftover_objects([network(object_id, "front")], *grammar)

    assert shown == LeftoverObject(object_id, "front", linkable)


def test_the_objects_shown_are_sorted_by_folded_name_and_cut_at_the_limit() -> None:
    objects = [network(f"{number:064x}", f"N{number:02d}") for number in range(25)]
    mixed = [network("1" * 64, "beta"), network("2" * 64, "Alpha")]

    assert [item.label for item in leftover_objects(mixed)] == ["Alpha", "beta"]
    assert len(leftover_objects(reversed(objects))) == MAX_LEFTOVERS_SHOWN


@pytest.mark.parametrize(
    ("name", "dismissible"),
    [
        pytest.param("shop", True, id="plain"),
        pytest.param("a" * 63, True, id="longest"),
        pytest.param("a" * 64, False, id="too-long"),
        pytest.param("has space", False, id="space"),
        pytest.param("-dash", False, id="leading-dash"),
    ],
)
def test_only_a_name_the_dismissal_can_be_stored_under_is_dismissible(
    name: str, dismissible: bool
) -> None:
    assert (DISMISSIBLE_NAME.fullmatch(name) is not None) is dismissible


async def test_a_project_of_only_leftovers_is_marked_and_one_with_a_stopped_container_is_not(
    stack_rig: Rig,
) -> None:
    host = stack_rig.host
    left = {PROJECT_LABEL: "Left"}
    host.add_volume("left_data", labels=left)
    network_id = host.add_network("left_default", 0x51, Labels=left)
    project_box(host, 1, "Held", state="exited")
    host.add_volume("held_data", labels={PROJECT_LABEL: "Held"})

    projects = {p.name: p for p in await stack_rig.fresh().queries.unmanaged(ADMIN, ENV_A)}

    left_only, held = projects["left"], projects["held"]
    assert left_only.leftovers_only is True
    assert (left_only.containers, left_only.volumes, left_only.networks) == (0, 1, 1)
    assert left_only.leftover_volumes == (
        LeftoverVolume("left_data", "left_data", VOLUME_CREATED, True),
    )
    assert left_only.leftover_networks == (LeftoverObject(network_id, "left_default", True),)
    assert left_only.dismissible is True and left_only.dismissal is None
    assert left_only.objects_digest == members_digest(
        [f"volume:left_data:{VOLUME_CREATED}", f"network:{network_id}"]
    )
    assert held.leftovers_only is False
    assert (held.containers, held.running, held.volumes) == (1, 0, 1)


async def test_hostile_volume_names_are_not_linkable_and_long_ones_keep_their_full_name(
    stack_rig: Rig,
) -> None:
    host = stack_rig.host
    odd = {PROJECT_LABEL: "odd"}
    host.add_volume(HOSTILE_NAME, labels=odd)
    host.add_volume(LONG_NAME, labels=odd)
    host.add_volume("spaced_data", labels={PROJECT_LABEL: "has space"})

    projects = {p.name: p for p in await stack_rig.fresh().queries.unmanaged(ADMIN, ENV_A)}

    shown = {item.name: item for item in projects["odd"].leftover_volumes}
    assert shown[HOSTILE_NAME].linkable is False
    assert shown[LONG_NAME] == LeftoverVolume(
        LONG_NAME, LONG_NAME[:LABEL_CUT], VOLUME_CREATED, True
    )
    assert projects["odd"].volumes == 2
    assert projects["has space"].dismissible is False


async def test_a_namespace_of_only_leftovers_is_marked_and_one_with_a_service_is_not(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    old = {NAMESPACE_LABEL: "Old"}
    config_id = stack_daemon.add_config("old-conf", labels=old)
    secret_id = stack_daemon.add_secret("old-sec", labels=old)
    network_id = swarm_network_id(7)
    stack_daemon.add_network(
        "old_net", 0, "overlay", network_id=network_id, Scope="swarm", Labels=old
    )
    stack_daemon.add_volume("old_data", labels=old)
    stack_daemon.add_service(plain_spec("live_web", Labels={NAMESPACE_LABEL: "Live"}))
    stack_daemon.add_config("live-conf", labels={NAMESPACE_LABEL: "Live"})
    queries = swarm_services(swarm_stack_rig).stacks

    page = await queries.page(ADMIN, ENV_A)
    detail = await queries.detail(ADMIN, ENV_A, "Old")
    live_detail = await queries.detail(ADMIN, ENV_A, "Live")

    rows = {row.name: row for row in page.rows}
    old_row, live_row = rows["Old"], rows["Live"]
    assert old_row.leftovers_only is True
    assert old_row.leftover_configs == (LeftoverObject(config_id, "old-conf", True),)
    assert old_row.leftover_secrets == (LeftoverObject(secret_id, "old-sec", True),)
    assert old_row.leftover_networks == (LeftoverObject(network_id, "old_net", True),)
    assert old_row.manager_volumes == (
        LeftoverVolume("old_data", "old_data", VOLUME_CREATED, True),
    )
    assert (old_row.manager_volumes_total, old_row.dismissible, old_row.dismissal) == (
        1,
        True,
        None,
    )
    assert old_row.objects_digest == members_digest(
        [f"config:{config_id}", f"secret:{secret_id}", f"network:{network_id}"]
    )
    assert live_row.leftovers_only is False and live_row.manager_volumes == ()
    assert detail.leftovers_only is True
    assert [v.name for v in detail.manager_volumes] == ["old_data"]
    assert live_detail.leftovers_only is False and live_detail.manager_volumes == ()


async def test_the_swarm_list_reads_the_manager_volumes_once_however_many_namespaces_are_leftovers(
    swarm_stack_rig: Rig, stack_daemon: StackDaemon
) -> None:
    for name in ("Alpha", "Beta", "Gamma"):
        stack_daemon.add_config(f"{name}-conf", labels={NAMESPACE_LABEL: name})
        stack_daemon.add_volume(f"{name}_data", labels={NAMESPACE_LABEL: name})
    queries = swarm_services(swarm_stack_rig).stacks
    before = len(stack_daemon.calls("GET", "/volumes"))

    page = await queries.page(ADMIN, ENV_A)

    assert [row.leftovers_only for row in page.rows] == [True, True, True]
    assert [row.manager_volumes_total for row in page.rows] == [1, 1, 1]
    assert len(stack_daemon.calls("GET", "/volumes")) - before == 1
