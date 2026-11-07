"""container-updater on Compose stacks: one redeploy after the pulls, and what it leaves alone."""

from typing import Any

import pytest

from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.image_updates import ImageUpdateService
from app.services.object_locks import LockHolder, ObjectKey
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import file_digest
from app.subsystems.container_updater import _Pass, _Pulled
from tests.access_support import ADMIN, ENV_A, STANDARD, digest_of
from tests.automation_support import counts
from tests.image_support import REFERENCE, digest, full_id, mark_missing
from tests.registry_support import ADDRESS
from tests.services.conftest import ImageRig  # noqa: F401
from tests.updater_support import (
    RECEIVER,
    SHOP,
    UPDATER,
    World,
    as_webhook,
    member,
    stack_selector,
)

STANDARD_ID = STANDARD.user_id
REPORT = {"action": "report"}
STALE_FILE_DIGEST = "a" * 64
PULL_POLICY_FILE = (
    f"services:\n  web:\n    image: {REFERENCE}\n    pull_policy: always\n"
    f"  worker:\n    image: {REFERENCE}\n"
)
BUDGET_MINUTES = 31


async def set_id_of(world: World) -> int:
    returned: int = (await world.db.fetch_one("SELECT id FROM registry_credential"))["id"]
    return returned


async def own_the_stack(world: World, owner: int = STANDARD_ID) -> None:
    """Make `owner` the owner of the stack and both members, without a grant on the set."""
    for number in (1, 2):
        await world.subject.grant(member(world, number), owner)
    stack = ResourceKey(ENV_A, "stack", SHOP)
    await world.rig.access.controls.create(
        ADMIN,
        stack,
        ControlSpec(access_level="write", owner_user_id=owner),
        expected_digest=await digest_of(world.rig.access, stack),
    )


async def test_one_pull_per_reference_then_one_redeploy_names_the_rule_and_run(
    shop: World,
) -> None:
    rule_id, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    detail = counts(result)
    assert (result.outcome, detail["pulled"], detail["updated"]) == ("success", 1, 1)
    assert shop.verbs().count("pull") == 1
    [pulled] = shop.rows("image_pulled")
    [redeployed] = shop.rows("stack_redeployed")
    for row in (pulled, redeployed):
        assert (row["actor_subsystem"], row["rule_id"]) == (UPDATER, rule_id)
    assert redeployed["detail"]["trigger"] == "update"


async def test_a_stack_among_several_names_is_redeployed_and_a_missing_one_is_reported(
    shop: World,
) -> None:
    _, job = await shop.rule(stack_selector("ghost", SHOP))

    result = await shop.run(job)

    detail = counts(result)
    assert (result.outcome, detail["updated"]) == ("success", 1)
    assert (detail["targets"], detail["target_outcomes"]) == (SHOP, f"{SHOP}: done")
    assert (detail["not_found"], detail["not_found_names"]) == (1, "ghost")
    assert len(shop.rows("stack_redeployed")) == 1


async def test_a_stack_the_owner_cannot_see_fails_the_run_as_missing_and_writes_nothing(
    shop: World,
) -> None:
    _, job = await shop.rule(stack_selector(), owner=STANDARD_ID)

    result = await shop.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert shop.rows() == [] and shop.verbs() == []


async def test_a_saved_edit_or_a_stopped_member_skips_the_stack_and_writes_nothing(
    shop: World,
) -> None:
    _, job = await shop.rule(stack_selector())
    await shop.db.execute(
        "UPDATE stack SET deployed_file_digest = ? WHERE name = ?", (STALE_FILE_DIGEST, SHOP)
    )
    pending = await shop.run(job)
    await shop.db.execute(
        "UPDATE stack SET deployed_file_digest = compose_file_digest, inputs_digest = ? "
        "WHERE name = ?",
        (STALE_FILE_DIGEST, SHOP),
    )
    pending_variables = await shop.run(job)
    await shop.db.execute(
        "UPDATE stack SET deployed_inputs_digest = inputs_digest WHERE name = ?", (SHOP,)
    )
    shop.host.boxes[member(shop, 2)].state = "exited"

    partly = await shop.run(job)

    assert counts(pending)["pending_changes"] == 1
    assert counts(pending_variables)["pending_changes"] == 1
    assert counts(partly)["partly_stopped"] == 1
    assert shop.rows() == [] and shop.verbs() == []


async def test_a_stack_with_no_running_container_is_skipped_not_running(shop: World) -> None:
    for number in (1, 2):
        shop.host.boxes[member(shop, number)].state = "exited"
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    assert (result.outcome, counts(result)["not_running"]) == ("success", 1)
    assert shop.rows() == []


async def test_a_stack_refusal_is_written_once_under_the_stack_name_and_then_held(
    shop: World,
) -> None:
    await mark_missing(shop.db, await set_id_of(shop))
    _, job = await shop.rule(stack_selector())

    first = await shop.run(job)
    shop.advance(10)
    held = await shop.run(job)

    assert first.reason == "credential_missing"
    [row] = shop.rows("image_update_refused")
    assert row["target_id"] == SHOP
    assert counts(held)["held"] == 1
    assert len(shop.rows("image_update_refused")) == 1
    assert shop.verbs() == []


async def test_a_local_image_newer_than_the_members_redeploys_without_a_pull(shop: World) -> None:
    shop.host.add_image(
        REFERENCE, full_id(2), config={"Cmd": ["serve"]}, digests=(f"{ADDRESS}/app@{digest(2)}",)
    )
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    assert counts(result)["local_newer"] == 2
    assert shop.rows("image_pulled") == []
    assert len(shop.rows("stack_redeployed")) == 1


async def test_a_dry_run_forecasts_the_stack_and_changes_nothing(shop: World) -> None:
    _, job = await shop.rule(stack_selector(), REPORT)

    result = await shop.run(job, dry=True)

    assert counts(result)["would_update"] == f"stack {SHOP}"
    assert shop.rows() == [] and shop.verbs() == []


async def test_the_start_budget_ending_during_the_pulls_defers_the_stack_before_the_redeploy(
    shop: World,
) -> None:
    async def slow_pull() -> None:
        shop.advance(BUDGET_MINUTES * 60)

    shop.host.on("pull", slow_pull)
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    detail = counts(result)
    assert detail["deferred"] == 1 and "failed" not in detail
    assert shop.rows("stack_redeployed") == []


async def test_a_user_acting_on_a_member_during_the_pulls_stops_the_redeploy(shop: World) -> None:
    locks = shop.subject.deps.engine.locks
    key = ObjectKey(ENV_A, "container", member(shop, 1))

    async def user_acts() -> None:
        async with locks.hold(key, LockHolder(UserActor(1), "someone", "stop")):
            pass

    shop.host.on("pull", user_acts)
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    assert (result.outcome, result.reason) == ("skipped", "suppressed")
    assert shop.rows("stack_redeployed") == []


async def test_a_standard_owner_without_a_grant_on_the_set_is_refused_on_the_check(
    shop: World,
) -> None:
    await own_the_stack(shop)
    _, job = await shop.rule(stack_selector(), owner=STANDARD_ID)

    result = await shop.run(job)

    assert result.reason == "credential_unavailable"
    assert shop.verbs() == []


async def test_the_compose_deploy_refuses_an_ungranted_set_even_if_the_check_let_it_by(
    shop: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = PULL_POLICY_FILE
    await shop.db.execute(
        "UPDATE stack SET compose_file = ?, compose_file_digest = ?, deployed_file_digest = ? "
        "WHERE name = ?",
        (text, file_digest(text), file_digest(text), SHOP),
    )
    await own_the_stack(shop)

    async def no_pull(self: Any, authority: Any, pull: Any, key: Any) -> Any:
        return _Pulled(digest(2), full_id(2).removeprefix("sha256:"))

    original = ImageUpdateService.check

    async def unguarded(
        self: Any,
        principal: Any,
        environment_id: int,
        references: Any,
        *,
        authorise_set: Any = None,
    ) -> Any:
        return await original(self, principal, environment_id, references)

    monkeypatch.setattr(_Pass, "_pull", no_pull)
    monkeypatch.setattr(ImageUpdateService, "check", unguarded)
    _, job = await shop.rule(stack_selector(), owner=STANDARD_ID)

    result = await shop.run(job)

    assert result.reason == "credential_unavailable"
    [row] = shop.rows("image_update_refused")
    assert row["target_id"] == SHOP


class TestPreview:
    selector = '{"kind":"stack","names":["shop"]}'

    async def test_a_stack_the_viewer_cannot_read_previews_empty_and_one_they_can_names_its_size(
        self, shop: World
    ) -> None:
        hidden = await shop.subject.subsystem.preview_targets(STANDARD, ENV_A, self.selector)
        listed = await shop.subject.subsystem.preview_targets(ADMIN, ENV_A, self.selector)

        assert hidden is not None and (hidden.names, hidden.others) == ((), 0)
        assert listed is not None
        assert (listed.names, listed.note) == ((SHOP,), "2 containers")

    async def test_the_size_note_is_singular_for_one_container(self, shop: World) -> None:
        shop.host.boxes.pop(member(shop, 2))

        listed = await shop.subject.subsystem.preview_targets(ADMIN, ENV_A, self.selector)

        assert listed is not None and listed.note == "1 container"


async def test_a_webhook_redeploy_redeploys_the_stack_without_a_pull_or_a_registry_request(
    shop: World,
) -> None:
    as_webhook(shop)
    rule_id, job = await shop.rule(stack_selector(), {"action": "redeploy", "verify_seconds": 0})

    result = await shop.subject.run(job, trigger="webhook")

    detail = counts(result)
    assert (result.outcome, detail["mode"], detail["redeployed"]) == ("success", "redeploy", 1)
    assert shop.manifests.calls == [] and "pull" not in shop.verbs()
    [redeployed] = shop.rows("stack_redeployed")
    assert (redeployed["actor_subsystem"], redeployed["rule_id"]) == (RECEIVER, rule_id)
    assert redeployed["detail"]["trigger"] == "webhook"
    assert redeployed["detail"]["webhook_action"] == "redeploy"


async def test_a_webhook_update_redeploys_the_stack_after_pulling_the_new_image(
    shop: World,
) -> None:
    as_webhook(shop)
    rule_id, job = await shop.rule(stack_selector(), {"action": "update", "verify_seconds": 0})

    result = await shop.subject.run(job, trigger="webhook")

    assert (result.outcome, counts(result)["updated"]) == ("success", 1)
    assert shop.verbs().count("pull") == 1
    [pulled] = shop.rows("image_pulled")
    [redeployed] = shop.rows("stack_redeployed")
    for row in (pulled, redeployed):
        assert (row["actor_subsystem"], row["rule_id"]) == (RECEIVER, rule_id)
        assert row["detail"]["trigger"] == "webhook"
