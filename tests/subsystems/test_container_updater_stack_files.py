"""container-updater leaves a stack whose companion files changed after its last deploy alone."""

from typing import Any

from app.db.repositories.stack import StackRepository
from app.db.repositories.stack_file import StackFileRepository
from tests.access_support import ENV_A
from tests.automation_support import counts
from tests.swarm_stack_support import NAME
from tests.swarm_updater_support import STACK, UPDATE, Shop, rows
from tests.updater_support import SHOP, World, stack_selector

EXTRA_FILE = "extra.env"
EXTRA_CONTENT = "A=1\n"
SECRET_FILE = "extra-secret.env"


async def write_files(rig: Any, stack: str) -> None:
    stacks = StackRepository(rig.db, rig.registry.vault, rig.clock)
    stored = await stacks.get(ENV_A, stack)
    assert stored is not None
    files = StackFileRepository(rig.db, rig.registry.vault, rig.clock, stacks=stacks)
    await files.put_file(stored.id, EXTRA_FILE, content=EXTRA_CONTENT, secret=False)
    await files.put_file(stored.id, SECRET_FILE, content=EXTRA_CONTENT, secret=True)


async def pending(rig: Any, stack: str) -> bool:
    row = await rig.db.fetch_one(
        "SELECT inputs_digest IS NOT deployed_inputs_digest AS pending FROM stack WHERE name = ?",
        (stack,),
    )
    return bool(row["pending"])


async def test_a_compose_stack_with_files_added_since_its_deploy_is_skipped_as_pending_changes(
    shop: World,
) -> None:
    before = await pending(shop.rig, SHOP)
    await write_files(shop.rig, SHOP)
    _, job = await shop.rule(stack_selector())

    result = await shop.run(job)

    assert (before, await pending(shop.rig, SHOP)) == (False, True)
    assert counts(result)["pending_changes"] == 1
    assert shop.rows() == [] and shop.verbs() == []


async def test_a_swarm_stack_with_files_added_since_its_deploy_is_skipped_as_pending_changes(
    swarm_shop: Shop,
) -> None:
    shop = swarm_shop
    shop.move()
    before = await pending(shop.rig, NAME)
    await write_files(shop.rig, NAME)
    _, job = await shop.subject.rule(STACK, UPDATE)
    sent = shop.sent()

    result = await shop.subject.run(job)

    assert (before, await pending(shop.rig, NAME)) == (False, True)
    assert counts(result)["pending_changes"] == 1
    assert rows(shop.subject) == [] and shop.sent() == sent
