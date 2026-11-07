"""Removing a stack: binding to the confirmation, kept and deleted volumes, and failure."""

from typing import Any

import pytest

from app.services.access import ResourceKey
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import ChangedSinceReview, StackOperationFailed
from app.services.stack_queries import VOLUME_REASON_CONTROLLED
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    MEMBER,
    STANDARD,
    control,
    digest_of,
    volume_key,
)
from tests.stack_support import (
    Rig,
    control_on_own_session,
    create_then_deploy,
    removal_binding,
    seed_stack_row,
)
from tests.support import present

SHOP = "shop"
STALE_DIGEST = "0" * 64
SHOP_FILE = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/var/lib/data
volumes:
  data: {}
"""
CONTROL_FILE = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/d1
      - held:/d2
      - late_control:/d3
volumes:
  data: {}
  held: {}
  late_control: {}
"""
LABEL_FILE = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/d1
      - late_label:/d2
volumes:
  data: {}
  late_label: {}
"""
FAILING_DOWN = SHOP_FILE + "x-fake:\n  down: fail\n"
DOWN_EXIT_CODE = 4


class ChangeAfterReview:
    """Alters volumes once, as the daemon receives the first volume removal."""

    def __init__(self, rig: Rig, *, controlled: str | None = None, unlabelled: str | None = None):
        self._rig = rig
        self._controlled = controlled
        self._unlabelled = unlabelled
        self._done = False

    async def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        if self._controlled:
            await control_on_own_session(self._rig, ENV_B, "volume", self._controlled)
        if self._unlabelled:
            self._rig.host.volume_labels[self._unlabelled] = {}


async def volume_controls(rig: Rig, environment_id: int) -> dict[str, Any]:
    rows = await rig.db.fetch_limited(
        "SELECT c.id, c.resource_id, c.owner_user_id, c.access_level FROM resource_control c "
        "WHERE c.environment_id = ? AND c.resource_type = 'volume'",
        (environment_id,),
        limit=100,
    )
    return {row["resource_id"]: row for row in rows}


async def test_remove_acts_only_on_what_its_confirmation_named_then_keeps_the_volumes_under_a_copy(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.fresh().actions, STANDARD, ENV_A, name=SHOP, compose_text=SHOP_FILE
    )
    stack = ResourceKey(ENV_A, "stack", SHOP)
    await rig.access.controls.update(
        ADMIN,
        stack,
        ControlSpec(
            access_level="write", owner_user_id=STANDARD.user_id, user_ids=[MEMBER.user_id]
        ),
        expected_digest=await digest_of(rig.access, stack),
    )
    review = await rig.fresh().queries.remove_confirmation(STANDARD, ENV_A, SHOP)
    mutations = len(rig.host.mutations())
    boxes = set(rig.host.boxes)

    bound = await removal_binding(rig, STANDARD, ENV_A, SHOP)
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.remove(
            STANDARD, ENV_A, SHOP, **bound | {"expected_stack_id": review.stack_id + 1}
        )
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.remove(
            STANDARD, ENV_A, SHOP, **bound | {"expected_containers": STALE_DIGEST}
        )
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.remove(
            STANDARD, ENV_A, SHOP, **bound | {"expected_networks": STALE_DIGEST}
        )
    refused_state = (len(rig.host.mutations()), set(rig.host.boxes))
    outcome = await rig.fresh().actions.remove(
        STANDARD,
        ENV_A,
        SHOP,
        expected_stack_id=review.stack_id,
        expected_containers=review.expected_containers,
        expected_networks=review.expected_networks,
    )

    assert refused_state == (mutations, boxes)
    assert (outcome.volumes_removed, outcome.volumes_kept) == ((), 1)
    assert rig.host.boxes == {}
    assert not any(n.get("Labels") for n in rig.host.networks.values() if n["Name"] != "bridge")
    assert "shop_data" in rig.host.volumes and rig.host.volume_removals() == []
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    assert (
        await rig.db.fetch_one("SELECT 1 FROM resource_control WHERE resource_type='stack'") is None
    )
    assert list(rig.compose.files.base.iterdir()) == []
    kept = (await volume_controls(rig, ENV_A))["shop_data"]
    assert (kept["owner_user_id"], kept["access_level"]) == (STANDARD.user_id, "write")
    grantees = await rig.db.fetch_limited(
        "SELECT user_id FROM resource_control_user_access WHERE resource_control_id = ?",
        (kept["id"],),
        limit=10,
    )
    assert [g["user_id"] for g in grantees] == [MEMBER.user_id]
    rows = await rig.rows("stack_removed")
    assert [r["result"] for r in rows] == ["failure", "failure", "failure", "success"]
    assert {r["detail"]["reason"] for r in rows[:3]} == {"changed_since_review"}
    assert rows[3]["detail"]["volumes_kept"] == 1 and rows[3]["detail"]["controls_copied"] == 1


async def test_removing_with_volumes_keeps_a_volume_that_gained_a_control_after_the_review(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(
        rig.fresh().actions, ADMIN, ENV_B, name=SHOP, compose_text=CONTROL_FILE
    )
    await control(rig.access, volume_key("shop_held", ENV_B))
    review = await rig.fresh().queries.remove_confirmation(ADMIN, ENV_B, SHOP, volumes=True)
    rig.host.on("volume_remove", ChangeAfterReview(rig, controlled="shop_late_control"))

    bound = await removal_binding(rig, ADMIN, ENV_B, SHOP, volumes=True)
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.remove(
            ADMIN, ENV_B, SHOP, **bound | {"expected_volumes": STALE_DIGEST}
        )
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.remove(
            ADMIN, ENV_B, SHOP, **{k: v for k, v in bound.items() if k != "expected_volumes"}
        )
    untouched = set(rig.host.volumes)
    outcome = await rig.fresh().actions.remove(ADMIN, ENV_B, SHOP, **bound)

    listed = {v.name: (v.delete, v.reason) for v in review.volumes}
    assert listed["shop_held"] == (False, VOLUME_REASON_CONTROLLED)
    assert {name for name, (delete, _) in listed.items() if delete} == {
        "shop_data",
        "shop_late_control",
    }
    assert untouched == {"shop_data", "shop_held", "shop_late_control"}
    assert outcome.volumes_removed == ("shop_data",) and outcome.volumes_kept == 2
    assert [name for name, _ in rig.host.volume_removals()] == ["shop_data"]
    assert [query for _, query in rig.host.volume_removals()] == [{"force": "false"}]
    assert set(rig.host.volumes) == {"shop_held", "shop_late_control"}
    row = (await rig.rows("stack_removed"))[-1]
    assert row["result"] == "success"
    assert row["detail"]["volumes_removed"] == 1
    assert row["detail"]["volumes_changed_since_review"] == 1


async def test_removing_with_volumes_keeps_a_volume_that_lost_the_project_label_after_the_review(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await create_then_deploy(rig.fresh().actions, ADMIN, ENV_B, name=SHOP, compose_text=LABEL_FILE)
    rig.host.on("volume_remove", ChangeAfterReview(rig, unlabelled="shop_late_label"))

    outcome = await rig.fresh().actions.remove(
        ADMIN, ENV_B, SHOP, **await removal_binding(rig, ADMIN, ENV_B, SHOP, volumes=True)
    )

    assert outcome.volumes_removed == ("shop_data",)
    assert "shop_late_label" in rig.host.volumes


async def test_a_failed_down_records_the_remove_as_a_failure_and_keeps_the_stack(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await seed_stack_row(rig, ENV_B, SHOP, FAILING_DOWN, owner_user_id=ADMIN.user_id)

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.remove(
            ADMIN, ENV_B, SHOP, **await removal_binding(rig, ADMIN, ENV_B, SHOP)
        )

    assert (failed.value.reason, failed.value.exit_code) == ("compose_failed", DOWN_EXIT_CODE)
    state = present(
        await rig.db.fetch_one(
            "SELECT status, last_operation, last_outcome FROM stack WHERE name = ?", (SHOP,)
        )
    )
    assert dict(state) == {"status": "error", "last_operation": "remove", "last_outcome": "failure"}
    assert await rig.db.fetch_one("SELECT 1 FROM resource_control WHERE resource_type='stack'")
    row = (await rig.rows("stack_removed"))[0]
    assert (row["result"], row["detail"]["reason"]) == ("failure", "compose_failed")
    assert len(rig.engine.locks) == 0


async def test_removing_a_compose_stack_keeps_its_control_while_a_namespace_still_folds_to_it(
    compose_on_swarm_rig: Rig, stack_daemon: Any
) -> None:
    rig = compose_on_swarm_rig
    await create_then_deploy(rig.fresh().actions, ADMIN, ENV_A, name=SHOP, compose_text=SHOP_FILE)
    stack_daemon.add_config("Shop_cfg", labels={"com.docker.stack.namespace": "Shop"})
    review = await rig.fresh().queries.remove_confirmation(ADMIN, ENV_A, SHOP)

    await rig.fresh().actions.remove(
        ADMIN,
        ENV_A,
        SHOP,
        expected_stack_id=review.stack_id,
        expected_containers=review.expected_containers,
        expected_networks=review.expected_networks,
    )

    controls = await rig.db.fetch_limited(
        "SELECT resource_id FROM resource_control WHERE resource_type = 'stack'", (), limit=5
    )
    assert [c["resource_id"] for c in controls] == [SHOP]
    [removed] = await rig.rows("stack_removed")
    assert (
        removed["detail"]["control_kept"] is True and removed["detail"]["control_released"] is False
    )


async def test_a_remove_that_names_nothing_it_was_shown_cannot_be_made(stack_rig: Rig) -> None:
    rig = stack_rig
    await create_then_deploy(rig.fresh().actions, ADMIN, ENV_B, name=SHOP, compose_text=SHOP_FILE)

    with pytest.raises(TypeError, match="expected_stack_id"):
        await rig.fresh().actions.remove(ADMIN, ENV_B, SHOP)  # type: ignore[call-arg]

    assert await rig.db.fetch_one("SELECT 1 FROM stack WHERE name = ?", (SHOP,)) is not None
    assert rig.host.volume_removals() == []
