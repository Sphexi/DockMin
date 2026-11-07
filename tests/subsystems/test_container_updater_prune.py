"""container-updater removes the images its updates replaced, once the grace period has passed."""

from datetime import timedelta
from typing import Any

import pytest

import app.subsystems.container_updater as updater
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.replaced_image import ReplacedImageRepository, ReplacedTargetType
from app.services import replaced_images
from app.services.object_locks import LockHolder, ObjectKey
from tests.access_support import ENV_A, STANDARD
from tests.automation_support import counts
from tests.docker_engine.container_engine import created_id
from tests.image_support import REFERENCE, digest, full_id, image_id
from tests.provisioning_support import old_body
from tests.registry_support import ADDRESS
from tests.updater_support import UPDATER, World, container, labelled

GRACE_HOURS = 1
GRACE_SECONDS = GRACE_HOURS * 3600
PRUNING = {
    "action": "update",
    "verify_seconds": 0,
    "prune_replaced": True,
    "prune_after_hours": GRACE_HOURS,
}
NOT_PRUNING = {"action": "update", "verify_seconds": 0}
REPLACED = 1
NEW_CONTAINER = created_id(1)
GONE_CONTAINER = f"{0xD0:064x}"
USER_CONTAINER = f"{0xD1:064x}"
REPLACEMENT = f"{0xD2:064x}"
UNUSED = 5
TAGGED = 6
DEPENDED_ON = 7
ONE_DAY = timedelta(days=1)
PAST_THE_AGE_OUT = timedelta(days=31)
RECORDS_PER_BATCH = replaced_images.MAX_EXAMINED + 1
FIRST_IN_USE = 100
NEWER = 200
FEW_DETAIL_KEYS = 6
SHORT_ID_LENGTH = 12
PINNED_REFERENCE = f"{ADDRESS}/app@{digest(50)}"
BY_ID_IMAGE = 9
PINNED_IMAGE = 10
SKIPPED = ("not_running", "swarm_managed", "compose_managed", "image_id_reference", "pinned")


def add_boxes_left_alone_for_five_reasons(world: World) -> None:
    network_id = next(iter(world.host.networks))
    stock_image(world, BY_ID_IMAGE)
    stock_image(world, PINNED_IMAGE, digests=(PINNED_REFERENCE,))
    padding = {"tier": "front"}
    swarmed = {**padding, "com.docker.swarm.service.id": "x"}
    composed = {**padding, "com.docker.compose.project": "p"}
    for offset, (state, labels, image) in enumerate(
        [
            ("exited", padding, REFERENCE),
            ("running", swarmed, REFERENCE),
            ("running", composed, REFERENCE),
            ("running", padding, full_id(BY_ID_IMAGE)),
            ("running", padding, PINNED_REFERENCE),
        ]
    ):
        body = old_body(network_id, config={"Image": image, "Labels": labels})
        world.host.add_configured(f"{0xF000 + offset:064x}", f"skip-{offset}", body, state)


@pytest.fixture
async def pruning(world: World) -> tuple[int, int]:
    """A rule that prunes, over a container whose image is already current, so only pruning runs."""
    world.manifests.default = digest(1)
    return await world.rule(container("web"), PRUNING)


async def record(
    world: World,
    rule_id: int,
    number: int,
    *,
    target_type: ReplacedTargetType = "container",
    target_id: str = GONE_CONTAINER,
) -> None:
    clock = world.rig.runtime.clock
    rule = await AutomationRuleRepository(world.db, clock).get(rule_id)
    assert rule is not None
    await ReplacedImageRepository(world.db, clock).add_replaced(
        rule, ENV_A, image_id(number), target_type, target_id
    )


async def waiting(world: World) -> list[str]:
    found = await world.db.fetch_limited(
        "SELECT image_id FROM replaced_image ORDER BY image_id", (), limit=1000
    )
    return [row["image_id"] for row in found]


def stock_image(world: World, number: int, reference: str | None = None, **fields: Any) -> None:
    world.host.add_image(reference, full_id(number), **fields)


def use_image(world: World, number: int, identifier: str = USER_CONTAINER) -> None:
    world.host.add(identifier, f"user-{number}", image=f"user-{number}", image_id=full_id(number))


def deletions(world: World) -> list[Any]:
    found: list[Any] = world.host.calls("DELETE")
    return found


def watch_locks(world: World) -> list[tuple[ObjectKey, LockHolder]]:
    held: list[tuple[ObjectKey, LockHolder]] = []
    suppress = world.subject.suppressions.observe

    def observe(key: ObjectKey, holder: LockHolder) -> None:
        held.append((key, holder))
        suppress(key, holder)

    world.rig.engine.locks.set_observer(observe)
    return held


async def after_grace(world: World, job: int, *, extra: timedelta = timedelta(0)) -> Any:
    world.advance(GRACE_SECONDS + extra.total_seconds())
    return await world.run(job)


async def test_a_replaced_image_is_kept_through_the_grace_period_and_then_removed_by_the_rule(
    world: World,
) -> None:
    world.new_image(2)
    rule_id, job = await world.rule(container("web"), PRUNING)
    replaced_at = format_timestamp(world.rig.runtime.clock())

    updated = await world.run(job)
    recorded = await waiting(world)
    world.advance(GRACE_SECONDS - 1)
    early = await world.run(job)
    kept_until_due = full_id(REPLACED) in world.host.image_store
    world.advance(1)
    due = await world.run(job)

    [removed] = world.rows("image_removed")
    assert counts(updated)["updated"] == 1
    assert recorded == [image_id(REPLACED)]
    assert kept_until_due and "pruned" not in counts(early)
    assert counts(due)["pruned"] == 1
    assert full_id(REPLACED) not in world.host.image_store
    assert (removed["actor_subsystem"], removed["rule_id"], removed["result"]) == (
        UPDATER,
        rule_id,
        "success",
    )
    detail = removed["detail"]
    assert (detail["trigger"], detail["replaced_target_type"]) == ("update_prune", "container")
    assert (detail["replaced_target"], detail["force"]) == (NEW_CONTAINER, False)
    assert detail["replaced_at"] == replaced_at
    assert await waiting(world) == []


async def test_the_removal_takes_the_image_and_reference_locks_and_is_never_forced(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED, digests=(f"{ADDRESS}/unused@{digest(UNUSED)}",))
    await record(world, rule_id, UNUSED)
    held = watch_locks(world)

    await after_grace(world, job)

    removals = [(key.object_type, holder.action) for key, holder in held]
    assert ("image", "remove") in removals
    assert ("image_reference", "remove") in removals
    assert {request.query.get("force") for request in deletions(world)} == {"false"}
    assert full_id(UNUSED) not in world.host.image_store


async def test_an_image_a_container_uses_is_kept_and_its_record_waits(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    use_image(world, UNUSED)
    await record(world, rule_id, UNUSED)

    result = await after_grace(world, job)

    assert counts(result)["prune_kept"] == 1
    assert full_id(UNUSED) in world.host.image_store
    assert await waiting(world) == [image_id(UNUSED)]
    assert world.rows("image_removed") == []


async def test_an_image_that_carries_a_tag_is_kept_and_its_record_is_dropped(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, TAGGED, f"{ADDRESS}/other:1")
    await record(world, rule_id, TAGGED)

    result = await after_grace(world, job)

    assert counts(result)["prune_kept"] == 1
    assert full_id(TAGGED) in world.host.image_store
    assert await waiting(world) == []
    assert deletions(world) == []


async def test_an_image_with_dependent_images_is_kept_and_its_record_is_dropped(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, DEPENDED_ON)
    await record(world, rule_id, DEPENDED_ON)
    world.host.fail("image_remove", 409)

    result = await after_grace(world, job)

    [refused] = world.rows("image_removed")
    assert counts(result)["prune_kept"] == 1
    assert (refused["result"], refused["detail"]["reason"]) == ("failure", "image_has_dependents")
    assert full_id(DEPENDED_ON) in world.host.image_store
    assert await waiting(world) == []


async def test_a_removal_the_host_refuses_is_counted_failed_and_its_record_is_kept(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)
    world.host.fail("image_remove", 500)

    result = await after_grace(world, job)

    [refused] = world.rows("image_removed")
    assert counts(result)["prune_failed"] == 1
    assert refused["result"] == "failure"
    assert full_id(UNUSED) in world.host.image_store
    assert await waiting(world) == [image_id(UNUSED)]


@pytest.mark.parametrize(
    "replacement",
    [{"state": "exited"}, {"health": "unhealthy"}],
    ids=["not-running", "unhealthy"],
)
async def test_an_image_whose_replacement_is_not_running_well_waits_until_it_is(
    world: World, pruning: tuple[int, int], replacement: dict[str, Any]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    box = world.host.add(REPLACEMENT, "web-new", image=REFERENCE, **replacement)
    await record(world, rule_id, UNUSED, target_id=REPLACEMENT)

    deferred = await after_grace(world, job)
    removals_while_deferred = world.rows("image_removed")
    box.state, box.health = "running", "healthy"
    world.advance(GRACE_SECONDS)
    settled = await world.run(job)

    assert counts(deferred)["prune_waiting"] == 1
    assert removals_while_deferred == []
    assert counts(settled)["pruned"] == 1
    assert full_id(UNUSED) not in world.host.image_store


async def test_a_record_past_the_age_out_is_dropped_without_removing_an_image_nothing_uses(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)

    result = await after_grace(world, job, extra=PAST_THE_AGE_OUT)

    assert counts(result)["prune_kept"] == 1
    assert full_id(UNUSED) in world.host.image_store
    assert await waiting(world) == []


async def test_a_record_past_the_age_out_is_dropped_even_while_a_container_uses_the_image(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    use_image(world, UNUSED)
    await record(world, rule_id, UNUSED)

    result = await after_grace(world, job, extra=PAST_THE_AGE_OUT)

    assert counts(result)["prune_kept"] == 1
    assert await waiting(world) == []


async def test_old_records_of_images_in_use_do_not_block_a_newer_one_that_is_due(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    for offset in range(RECORDS_PER_BATCH):
        number = FIRST_IN_USE + offset
        stock_image(world, number)
        use_image(world, number, f"{0xE000 + offset:064x}")
        await record(world, rule_id, number)
    world.advance(PAST_THE_AGE_OUT.total_seconds() - ONE_DAY.total_seconds())
    stock_image(world, NEWER)
    await record(world, rule_id, NEWER)
    world.advance(2 * ONE_DAY.total_seconds())

    first = await world.run(job)
    second = await world.run(job)

    assert counts(first)["prune_kept"] == replaced_images.MAX_EXAMINED
    assert counts(second)["pruned"] == 1
    assert full_id(NEWER) not in world.host.image_store
    assert await waiting(world) == []


async def test_a_grace_period_of_thirty_days_still_reaches_its_removal(world: World) -> None:
    world.manifests.default = digest(1)
    month = {**PRUNING, "prune_after_hours": 720}
    rule_id, job = await world.rule(container("web"), month)
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)
    world.advance(timedelta(days=30).total_seconds())

    result = await world.run(job)

    assert counts(result)["pruned"] == 1
    assert full_id(UNUSED) not in world.host.image_store


async def test_a_dry_run_names_what_it_would_remove_and_removes_and_writes_nothing(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)
    world.advance(GRACE_SECONDS)

    result = await world.run(job, dry=True)

    detail = counts(result)
    assert (detail["would_prune"], detail["would_prune_images"]) == (
        1,
        image_id(UNUSED)[:SHORT_ID_LENGTH],
    )
    assert "pruned" not in detail
    assert deletions(world) == [] and world.rows() == []
    assert await waiting(world) == [image_id(UNUSED)]


async def test_a_rule_that_does_not_prune_leaves_its_records_and_images_alone(
    world: World,
) -> None:
    world.manifests.default = digest(1)
    rule_id, job = await world.rule(container("web"), NOT_PRUNING)
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)

    result = await after_grace(world, job)

    assert "pruned" not in counts(result) and "prune_kept" not in counts(result)
    assert full_id(UNUSED) in world.host.image_store
    assert await waiting(world) == [image_id(UNUSED)]


async def test_a_stored_rule_whose_owner_is_not_an_administrator_removes_nothing(
    world: World,
) -> None:
    world.manifests.default = digest(1)
    rule_id, job = await world.rule(labelled(), PRUNING, owner=STANDARD.user_id)
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)

    result = await after_grace(world, job)

    assert result.outcome == "success"
    assert full_id(UNUSED) in world.host.image_store
    assert world.rows("image_removed") == []
    assert await waiting(world) == [image_id(UNUSED)]


async def test_records_stored_under_another_creation_of_the_rule_are_dropped_not_acted_on(
    world: World, pruning: tuple[int, int]
) -> None:
    rule_id, job = pruning
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)
    await world.db.execute(
        "UPDATE replaced_image SET rule_created_at = '2001-01-01T00:00:00Z' WHERE rule_id = ?",
        (rule_id,),
    )

    await after_grace(world, job)

    assert full_id(UNUSED) in world.host.image_store
    assert await waiting(world) == []


async def test_the_prune_counts_survive_a_run_whose_skip_reasons_fill_the_detail(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.manifests.default = digest(1)
    rule_id, job = await world.rule(labelled(), PRUNING)
    stock_image(world, UNUSED)
    await record(world, rule_id, UNUSED)
    add_boxes_left_alone_for_five_reasons(world)
    world.advance(GRACE_SECONDS)
    full = await world.run(job, dry=False)
    monkeypatch.setattr(updater, "MAX_RUN_DETAIL_KEYS", FEW_DETAIL_KEYS)
    await record(world, rule_id, TAGGED)
    stock_image(world, TAGGED)
    world.advance(GRACE_SECONDS)

    cut = await world.run(job)

    assert {reason for reason in SKIPPED if reason in counts(full)} == set(SKIPPED)
    assert len(counts(cut)) <= FEW_DETAIL_KEYS
    assert counts(cut)["pruned"] == 1
    assert {reason for reason in SKIPPED if reason in counts(cut)} != set(SKIPPED)
