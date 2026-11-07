"""container-updater keeps a replaced image that a Swarm service still names by digest."""

from collections.abc import Iterator
from datetime import timedelta

import pytest

from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.replaced_image import ReplacedImageRepository
from tests.access_support import ENV_A
from tests.automation_support import counts
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.image_support import digest, full_id, image_id
from tests.registry_support import ADDRESS
from tests.updater_support import World, container

GRACE_HOURS = 1
PRUNING = {
    "action": "update",
    "verify_seconds": 0,
    "prune_replaced": True,
    "prune_after_hours": GRACE_HOURS,
}
REPLACED = 5
NAMED_DIGEST = digest(REPLACED)
OTHER_DIGEST = digest(6)
REPOSITORY = f"{ADDRESS}/unused"
GONE_CONTAINER = f"{0xD0:064x}"
CURRENT_IMAGE = "busybox:1"


@pytest.fixture
def host(swarm_host: SwarmEngine) -> Iterator[SwarmEngine]:
    yield swarm_host


async def waiting_with_the_image_stocked(world: World) -> int:
    world.manifests.default = digest(1)
    rule_id, job = await world.rule(container("web"), PRUNING)
    world.host.add_image(None, full_id(REPLACED), digests=(f"{REPOSITORY}@{NAMED_DIGEST}",))
    clock = world.rig.runtime.clock
    rule = await AutomationRuleRepository(world.db, clock).get(rule_id)
    assert rule is not None
    await ReplacedImageRepository(clock=clock, session=world.db).add_replaced(
        rule, ENV_A, image_id(REPLACED), "container", GONE_CONTAINER
    )
    world.advance(timedelta(hours=GRACE_HOURS).total_seconds())
    return job


def name_digest(host: SwarmEngine, digest_named: str, *, as_previous: bool) -> None:
    named = plain_spec("web", f"{REPOSITORY}:1@{digest_named}")
    if as_previous:
        service_id = host.add_service(plain_spec("web", CURRENT_IMAGE))
        host.services[service_id]["PreviousSpec"] = named
    else:
        host.add_service(named)


@pytest.mark.parametrize("as_previous", [False, True], ids=["spec", "previous-spec"])
async def test_a_service_naming_the_images_digest_keeps_it_and_its_record(
    world: World, host: SwarmEngine, as_previous: bool
) -> None:
    job = await waiting_with_the_image_stocked(world)
    name_digest(host, NAMED_DIGEST, as_previous=as_previous)

    result = await world.run(job)

    assert counts(result)["prune_kept"] == 1
    assert full_id(REPLACED) in host.image_store
    assert world.rows("image_removed") == []
    assert (await world.db.fetch_one("SELECT COUNT(*) AS found FROM replaced_image"))["found"] == 1


async def test_a_service_naming_another_digest_does_not_keep_the_image(
    world: World, host: SwarmEngine
) -> None:
    job = await waiting_with_the_image_stocked(world)
    name_digest(host, OTHER_DIGEST, as_previous=False)

    result = await world.run(job)

    assert counts(result)["pruned"] == 1
    assert full_id(REPLACED) not in host.image_store


async def test_a_services_reference_without_a_digest_does_not_keep_the_image(
    world: World, host: SwarmEngine
) -> None:
    job = await waiting_with_the_image_stocked(world)
    host.add_service(plain_spec("web", f"{REPOSITORY}:1"))

    result = await world.run(job)

    assert counts(result)["pruned"] == 1
    assert full_id(REPLACED) not in host.image_store
