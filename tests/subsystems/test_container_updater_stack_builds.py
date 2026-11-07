"""Automation never builds: the updater skips a stack that builds, a webhook redeploy reuses it."""

from app.services.stack_deploy import file_digest
from tests.access_support import ADMIN, ENV_A
from tests.automation_support import counts
from tests.stack_build_support import CONTEXT, DOCKERFILE, add_context
from tests.updater_support import RECEIVER, SHOP, World, as_webhook, stack_selector

BUILDING = f"services:\n  web:\n    build: ./{CONTEXT}\n  worker:\n    image: nginx:1\n"
REDEPLOY = {"action": "redeploy", "verify_seconds": 0}
UPDATE = {"action": "update", "verify_seconds": 0}


async def make_it_build(shop: World) -> None:
    """Store a file with a service that builds as the deployed one, with its context."""
    await shop.db.execute(
        "UPDATE stack SET compose_file = ?, compose_file_digest = ?, "
        "deployed_file_digest = ? WHERE name = ?",
        (BUILDING, file_digest(BUILDING), file_digest(BUILDING), SHOP),
    )
    row = await shop.db.fetch_one("SELECT id FROM stack WHERE name = ?", (SHOP,))
    assert row is not None
    await add_context(shop.rig, row["id"], CONTEXT, {"Dockerfile": DOCKERFILE})
    await shop.db.execute(
        "UPDATE stack SET deployed_inputs_digest = inputs_digest WHERE name = ?", (SHOP,)
    )


async def test_the_updater_skips_a_stack_that_builds_and_writes_nothing(shop: World) -> None:
    await make_it_build(shop)
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    assert counts(result)["built_image"] == 1
    assert shop.rows() == [] and shop.verbs() == []


async def test_a_webhook_update_skips_it_and_a_webhook_redeploy_reuses_the_built_image(
    shop: World,
) -> None:
    await make_it_build(shop)
    assert shop.rig.buildx is not None
    built = await shop.rig.fresh().actions.redeploy(ADMIN, ENV_A, SHOP)
    assert shop.rig.buildx.counter == 1 and built.built
    as_webhook(shop)
    _, update_job = await shop.rule(stack_selector(), UPDATE)

    skipped = await shop.subject.run(update_job, trigger="webhook")
    rule_id, redeploy_job = await shop.rule(stack_selector(), REDEPLOY, name="second")
    redeployed = await shop.subject.run(redeploy_job, trigger="webhook")

    assert counts(skipped)["built_image"] == 1
    assert (redeployed.outcome, counts(redeployed)["redeployed"]) == ("success", 1)
    assert shop.rig.buildx.counter == 1
    [row] = [r for r in shop.rows("stack_redeployed") if r["rule_id"] == rule_id]
    assert row["actor_subsystem"] == RECEIVER
    assert row["detail"]["built"].endswith(":reused")
