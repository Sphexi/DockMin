"""The health check, process, host, security, limits and logging fields through create and edit."""

from typing import Any

import pytest

from app.services.swarm_service_views import RestrictedService
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.object_support import rows_of
from tests.services.conftest import SwarmRig
from tests.services.test_service_spec_task import EVERYTHING, SECTIONS

FORM = {"name": "web", "image": "busybox:latest", "mode": "replicated", "replicas": "1"}
STANDARD_FIELDS = {
    key: value
    for section in ("healthcheck", "process", "host", "ulimits")
    for key, value in SECTIONS[section].items()
} | {"read_only": "on", "cap_drop": "ALL", "log_driver": "local", "logopt-0-key": "max-size"}


def container_of(rig: SwarmRig, service_id: str) -> dict[str, Any]:
    stored: dict[str, Any] = rig.host.services[service_id]["Spec"]["TaskTemplate"]["ContainerSpec"]
    return stored


async def deploy(rig: SwarmRig, who: Any, **fields: str) -> str:
    created = await rig.sw.service_actions.create(who, ENV_A, {**FORM, **fields})
    rig.fresh()
    return created.service_id


async def test_an_administrator_sets_every_section_and_an_unchanged_edit_sends_nothing(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm, ADMIN, **EVERYTHING)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    before = len(swarm.host.mutating())

    review = await swarm.sw.services.review(
        ADMIN, ENV_A, service_id, dict(offer.values), form_version=offer.version
    )
    updated = await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, dict(offer.values), expected_version=offer.version
    )

    stored = container_of(swarm, service_id)
    assert stored["CapabilityAdd"] == ["CAP_NET_BIND_SERVICE", "CAP_SYS_TIME"]
    assert stored["Sysctls"] == {"net.core.somaxconn": "1024", "kernel.shmmax": "65536"}
    assert stored["Healthcheck"]["Test"] == ["CMD", "curl", "-f", "http://localhost/a b"]
    assert swarm.host.services[service_id]["Spec"]["TaskTemplate"]["LogDriver"]["Name"] == (
        "json-file"
    )
    assert review.categories == () and not review.tasks_replaced
    assert updated.unchanged and updated.categories == ()
    assert len(swarm.host.mutating()) == before


async def test_editing_the_health_interval_changes_that_category_and_replaces_tasks(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm, ADMIN, **EVERYTHING)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    values = {**offer.values, "health_interval": "45s"}

    review = await swarm.sw.services.review(
        ADMIN, ENV_A, service_id, values, form_version=offer.version
    )
    updated = await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, values, expected_version=offer.version
    )

    row = (await rows_of(swarm.db, "service_updated"))[-1]
    assert review.categories == ("healthcheck",) and review.tasks_replaced
    assert updated.categories == ("healthcheck",) and not updated.unchanged
    assert row["detail"]["changed"] == "healthcheck"
    assert container_of(swarm, service_id)["Healthcheck"]["Interval"] == 45_000_000_000


async def test_a_standard_user_sets_the_sections_they_may_and_edits_them(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm, STANDARD, **STANDARD_FIELDS)
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)

    review = await swarm.sw.services.review(
        STANDARD, ENV_A, service_id, dict(offer.values), form_version=offer.version
    )
    await swarm.sw.service_actions.update(
        STANDARD,
        ENV_A,
        service_id,
        {**offer.values, "stop_grace_period": "31s"},
        expected_version=offer.version,
    )

    assert review.categories == ()
    assert "cap_add" not in offer.values and "sysctls" not in offer.values
    assert container_of(swarm, service_id)["StopGracePeriod"] == 31_000_000_000
    assert container_of(swarm, service_id)["ReadOnly"] is True


@pytest.mark.parametrize(
    ("posted", "option"),
    [
        ({"cap_add": "NET_ADMIN"}, "cap_add"),
        ({"sysctls": "net.core.somaxconn=1"}, "sysctls"),
        ({"log_driver": "syslog"}, "log_driver"),
    ],
)
async def test_a_value_a_standard_user_posts_for_a_refused_field_creates_nothing(
    swarm: SwarmRig, posted: dict[str, str], option: str
) -> None:
    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.create(STANDARD, ENV_A, {**FORM, **posted})

    assert option in raised.value.refused and swarm.host.services == {}


async def test_a_standard_users_edit_carries_a_stored_value_and_the_walker_still_judges_it(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm, STANDARD)
    container_of(swarm, service_id).update(
        CapabilityAdd=["CAP_NET_ADMIN"], Sysctls={"net.core.somaxconn": "9"}
    )
    swarm.host.services[service_id]["Version"]["Index"] += 1
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(STANDARD, ENV_A, service_id)
    before = len(swarm.host.mutating())

    with pytest.raises(RestrictedService) as raised:
        await swarm.sw.service_actions.update(
            STANDARD,
            ENV_A,
            service_id,
            {**offer.values, "user": "1000"},
            expected_version=offer.version,
        )

    assert offer.values["locked_cap_add"] == "CAP_NET_ADMIN"
    assert "cap_add" not in offer.values and "sysctls" not in offer.values
    assert set(raised.value.refused) == {"cap_add", "sysctls"}
    assert len(swarm.host.mutating()) == before
    assert container_of(swarm, service_id)["CapabilityAdd"] == ["CAP_NET_ADMIN"]
