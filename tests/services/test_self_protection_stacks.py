"""Compose stacks that hold, or are named for, the container dockmin runs in."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound, SelfProtected
from app.services.activity import ActivityRecorder
from app.services.assembly import (
    StackServices,
    build_access_services,
    build_host_clients,
    build_stack_services,
)
from app.services.object_inspector import EngineObjectInspector
from app.services.stack_deploy import ChangedSinceReview
from app.services.stack_queries import (
    VOLUME_REASON_FOREIGN,
    VOLUME_REASON_IN_USE,
    VOLUME_REASON_SELF,
)
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import STANDARD_ID
from tests.self_support import Held, home_identity, self_protected_rows
from tests.stack_support import (
    PROJECT_LABEL,
    Rig,
    create_then_deploy,
    project_box,
    read_records,
    removal_binding,
    seed_stack_row,
)

SHOP = "shop"
SHOP_VOLUME = "shop_data"
FIRST_MEMBER = f"{0xA0001:064x}"
STALE = "0" * 64
VOLUME_FILE = """
services:
  web:
    image: nginx:1
    volumes:
      - data:/data
volumes:
  data: {}
"""

type Operation = Callable[[StackServices], Awaitable[Any]]

OPERATIONS: dict[str, tuple[Operation, str]] = {
    "stop": (lambda s: s.actions.stop(ADMIN, ENV_A, SHOP), "stack_stopped"),
    "deploy": (lambda s: s.actions.deploy(ADMIN, ENV_A, SHOP), "stack_deployed"),
    "redeploy": (lambda s: s.actions.redeploy(ADMIN, ENV_A, SHOP), "stack_redeployed"),
    "remove": (
        lambda s: s.actions.remove(
            ADMIN,
            ENV_A,
            SHOP,
            expected_stack_id=1,
            expected_containers=STALE,
            expected_networks=STALE,
        ),
        "stack_removed",
    ),
}


def stack_file(record: Path) -> str:
    return (
        "services:\n  db:\n    image: nginx:1\n  web:\n    image: nginx:1\n"
        f"x-fake:\n  record: {record}\n"
    )


def guarded(rig: Rig, held: Held) -> StackServices:
    """Build the stack services for a new request around an identity the test can change."""
    recorder = ActivityRecorder(rig.db, rig.clock)
    inspector = EngineObjectInspector(build_host_clients(rig.db, rig.runtime, rig.engine))
    access = build_access_services(rig.db, rig.runtime, inspector, recorder)
    return build_stack_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine,
        rig.registry.registries,
        access,
        inspector,
        recorder,
        self_identity=held,
    )


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "compose-record.jsonl"


@pytest.fixture
async def shop(stack_rig: Rig, record: Path) -> Rig:
    await seed_stack_row(stack_rig, ENV_A, SHOP, stack_file(record))
    project_box(stack_rig.host, 1, SHOP, "web")
    project_box(stack_rig.host, 2, SHOP, "db", state="exited")
    return stack_rig


@pytest.mark.parametrize("operation", list(OPERATIONS))
async def test_an_operation_on_the_stack_dockmin_belongs_to_is_refused_before_compose_runs(
    shop: Rig, record: Path, operation: str
) -> None:
    call, event = OPERATIONS[operation]
    services = guarded(shop, Held(home_identity(compose_project=SHOP)))
    before = set(shop.host.boxes)

    with pytest.raises(SelfProtected) as refused:
        await call(services)

    [row] = await self_protected_rows(shop.db)
    assert str(refused.value) == str(SelfProtected.stack(SHOP))
    assert (row["action"], row["target_id"], row["environment_id"]) == (event, SHOP, ENV_A)
    assert row["detail"]["message"] == str(SelfProtected.stack(SHOP))
    assert read_records(record) == []
    assert shop.host.mutations() == []
    assert set(shop.host.boxes) == before
    assert len(shop.engine.locks) == 0


async def test_a_stack_holding_dockmins_container_is_refused_although_its_name_is_not_recorded(
    shop: Rig, record: Path
) -> None:
    services = guarded(shop, Held(home_identity(container=FIRST_MEMBER)))

    with pytest.raises(SelfProtected):
        await services.actions.stop(ADMIN, ENV_A, SHOP)

    [row] = await self_protected_rows(shop.db)
    assert row["action"] == "stack_stopped"
    assert read_records(record) == []
    assert shop.host.mutations() == []


async def test_starting_the_stack_dockmin_belongs_to_is_allowed(shop: Rig, record: Path) -> None:
    services = guarded(shop, Held(home_identity(compose_project=SHOP)))

    outcome = await services.actions.start(ADMIN, ENV_A, SHOP)

    assert outcome.operation == "start"
    assert [call["cmd"] for call in read_records(record) if call["cmd"] == "start"] == ["start"]
    assert await self_protected_rows(shop.db) == []


async def test_another_stack_is_not_affected_by_the_identity(shop: Rig, record: Path) -> None:
    services = guarded(shop, Held(home_identity(compose_project="unrelated")))

    outcome = await services.actions.stop(ADMIN, ENV_A, SHOP)

    assert outcome.operation == "stop"
    assert await self_protected_rows(shop.db) == []


@pytest.mark.parametrize("confirmation", ["remove", "redeploy"])
async def test_the_confirmation_for_dockmins_stack_is_refused_up_front(
    shop: Rig, confirmation: str
) -> None:
    queries = guarded(shop, Held(home_identity(compose_project=SHOP))).queries

    with pytest.raises(SelfProtected):
        if confirmation == "remove":
            await queries.remove_confirmation(ADMIN, ENV_A, SHOP)
        else:
            await queries.redeploy_confirmation(ADMIN, ENV_A, SHOP)


async def test_a_user_who_cannot_see_the_stack_gets_the_not_found_any_hidden_stack_gives(
    shop: Rig, record: Path
) -> None:
    await seed_stack_row(shop, ENV_A, "other", stack_file(record))
    services = guarded(shop, Held(home_identity(compose_project=SHOP)))

    with pytest.raises(ResourceNotFound) as hidden:
        await services.actions.stop(STANDARD, ENV_A, "other")
    with pytest.raises(ResourceNotFound) as protected:
        await services.actions.stop(STANDARD, ENV_A, SHOP)

    assert str(protected.value) == str(hidden.value)
    assert await self_protected_rows(shop.db) == []


class TestVolumesOfTheStack:
    @pytest.fixture
    async def owned(self, stack_rig: Rig) -> Rig:
        await seed_stack_row(stack_rig, ENV_A, SHOP, VOLUME_FILE, owner_user_id=STANDARD_ID)
        return stack_rig

    @pytest.mark.parametrize(
        ("who", "labelled", "reason"),
        [
            pytest.param(ADMIN, True, VOLUME_REASON_SELF, id="admin-project-volume"),
            pytest.param(ADMIN, False, VOLUME_REASON_SELF, id="admin-unlabelled-volume"),
            pytest.param(STANDARD, True, VOLUME_REASON_IN_USE, id="user-project-volume"),
            pytest.param(STANDARD, False, VOLUME_REASON_FOREIGN, id="user-unlabelled-volume"),
        ],
    )
    async def test_a_volume_dockmin_has_mounted_is_kept_with_a_reason_that_depends_on_the_reader(
        self, owned: Rig, who: Any, labelled: bool, reason: str
    ) -> None:
        owned.host.add_volume(SHOP_VOLUME, labels={PROJECT_LABEL: SHOP} if labelled else {})
        services = guarded(owned, Held(home_identity(volumes=(SHOP_VOLUME,))))

        review = await services.queries.remove_confirmation(who, ENV_A, SHOP, volumes=True)

        [entry] = review.volumes
        assert (entry.name, entry.delete, entry.reason) == (SHOP_VOLUME, False, reason)

    async def test_a_volume_that_became_dockmins_after_the_review_makes_the_review_stale(
        self, stack_rig: Rig
    ) -> None:
        rig = stack_rig
        await create_then_deploy(
            rig.fresh().actions, ADMIN, ENV_A, name=SHOP, compose_text=VOLUME_FILE
        )
        held = Held()
        review = await guarded(rig, held).queries.remove_confirmation(
            ADMIN, ENV_A, SHOP, volumes=True
        )
        held.identity = home_identity(volumes=(SHOP_VOLUME,))
        bound = await removal_binding(rig, ADMIN, ENV_A, SHOP, volumes=True)

        with pytest.raises(ChangedSinceReview):
            await guarded(rig, held).actions.remove(ADMIN, ENV_A, SHOP, **bound)

        assert [v.name for v in review.volumes if v.delete] == [SHOP_VOLUME]
        assert SHOP_VOLUME in rig.host.volumes
        assert rig.host.volume_removals() == []
        assert rig.host.boxes != {}
