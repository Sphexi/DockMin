"""Services count as users of the overlay networks they join, in lists, details and removals."""

import pytest

from app.services.access import ResourceKey
from app.services.assembly import NetworkServices, build_network_services
from app.services.networks import NetworkChangedSinceReview, NetworkFilters, NetworkInUse
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import ADMIN, ENV_A, STANDARD, control
from tests.docker_engine.container_engine import swarm_network_id
from tests.services.conftest import SwarmRig

OVERLAY = swarm_network_id(5)


def networks_of(rig: SwarmRig) -> NetworkServices:
    return build_network_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.access,
        rig.inspector,
        rig.recorder,
    )


@pytest.fixture
def overlay(swarm: SwarmRig) -> str:
    return swarm.host.add_network(
        "ov", 0, "overlay", network_id=OVERLAY, Scope="swarm", Attachable=True
    )


def join(swarm: SwarmRig) -> dict[str, str]:
    return {
        "by-id": swarm.host.named_service("by-id", networks=[OVERLAY]),
        "by-name": swarm.host.named_service("by-name", networks=["ov"]),
        "by-vip": swarm.host.named_service("by-vip", vips=[OVERLAY]),
        "elsewhere": swarm.host.named_service("elsewhere"),
    }


async def test_services_naming_a_network_by_id_name_or_virtual_address_are_its_users(
    swarm: SwarmRig, overlay: str
) -> None:
    join(swarm)
    nets = networks_of(swarm)

    page = await nets.queries.page(ADMIN, ENV_A, NetworkFilters(q="ov"))
    used = await nets.queries.page(ADMIN, ENV_A, NetworkFilters(usage="used"))
    detail = await nets.queries.detail(ADMIN, ENV_A, overlay)

    [row] = page.page.items
    assert (row.in_use, row.service_count, row.container_count) == (3, 3, 0)
    assert [s.name for s in row.services] == ["by-id", "by-name", "by-vip"] and row.services_known
    assert "ov" in {r.name for r in used.page.items}
    assert (detail.in_use, detail.service_count, len(detail.services)) == (3, 3, 3)


async def test_the_removal_confirmation_names_services_and_binds_the_count(
    swarm: SwarmRig, overlay: str
) -> None:
    join(swarm)
    nets = networks_of(swarm)

    impact = await nets.queries.remove_impact(ADMIN, ENV_A, overlay)
    with pytest.raises(NetworkChangedSinceReview):
        await nets.actions.remove(ADMIN, ENV_A, overlay, expected_name="ov", expected_in_use=0)
    with pytest.raises(NetworkInUse):
        await nets.actions.remove(ADMIN, ENV_A, overlay, expected_name="ov", expected_in_use=3)

    assert (impact.in_use, impact.service_count, impact.container_count) == (3, 3, 0)
    assert impact.services_hidden == 0 and overlay in swarm.host.networks


async def test_a_standard_user_is_told_only_the_services_they_may_see_and_how_many_are_hidden(
    swarm: SwarmRig, overlay: str
) -> None:
    services = join(swarm)
    await control(
        swarm.access, ResourceKey(ENV_A, "service", services["by-id"]), users=(STANDARD.user_id,)
    )
    await control(
        swarm.access,
        ResourceKey(ENV_A, "network", overlay),
        level="write",
        users=(STANDARD.user_id,),
    )
    swarm.fresh()

    seen = await networks_of(swarm).queries.remove_impact(STANDARD, ENV_A, overlay)

    assert seen.in_use == 3 and [s.name for s in seen.services] == ["by-id"]
    assert seen.services_hidden == 2


async def test_prune_leaves_a_network_a_service_uses_and_takes_an_unused_one(
    swarm: SwarmRig, overlay: str
) -> None:
    join(swarm)
    local = swarm.host.add_network("local-only", 0x77)

    plan = await networks_of(swarm).queries.prune_plan(ADMIN, ENV_A)

    assert {c.name for c in plan.candidates} == {"local-only"}
    assert local in swarm.host.networks and overlay in swarm.host.networks


async def test_once_no_service_uses_it_the_network_is_free(swarm: SwarmRig, overlay: str) -> None:
    join(swarm)
    swarm.host.services.clear()

    impact = await networks_of(swarm).queries.remove_impact(ADMIN, ENV_A, overlay)

    assert impact.in_use == 0


async def test_a_standalone_host_reads_no_services(swarm: SwarmRig, overlay: str) -> None:
    join(swarm)
    await swarm.db.execute("UPDATE environment SET type = 'standalone'")
    swarm.fresh()
    swarm.host.requests.clear()

    page = await networks_of(swarm).queries.page(ADMIN, ENV_A)

    assert [r.services for r in page.page.items if r.name == "ov"] == [()]
    assert swarm.host.calls("GET", "/services") == []


async def test_a_service_list_that_cannot_be_read_marks_use_unknown_in_views_and_fails_closed(
    swarm: SwarmRig, overlay: str
) -> None:
    join(swarm)
    swarm.host.outage = {"services"}
    nets = networks_of(swarm)

    page = await nets.queries.page(ADMIN, ENV_A)
    detail = await nets.queries.detail(ADMIN, ENV_A, overlay)
    with pytest.raises(EnvironmentUnavailable):
        await nets.queries.remove_impact(ADMIN, ENV_A, overlay)
    with pytest.raises(EnvironmentUnavailable):
        await nets.queries.prune_plan(ADMIN, ENV_A)

    [row] = [r for r in page.page.items if r.name == "ov"]
    assert not row.services_known and row.service_count == 0
    assert not detail.services_known
    assert swarm.poker.poked
