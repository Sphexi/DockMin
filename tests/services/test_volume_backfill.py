"""The volume owner backfill: candidates, the preview's outcomes, and the audited apply."""

from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.db.repositories.activity_event import Outcome
from app.db.repositories.resource_control import ResourceControlRepository, ResourceType
from app.services.activity import UserActor
from app.services.volume_backfill import (
    BackfillChanged,
    BackfillPlan,
    VolumeBackfillService,
    build_volume_backfill,
)
from tests.access_support import (
    ADMIN,
    ENV_A,
    MEMBER_ID,
    MEMBER_USERNAME,
    STANDARD,
    container_id,
    volume_key,
)
from tests.auth_support import STANDARD_ID, STANDARD_USERNAME
from tests.docker_engine.container_engine import ContainerEngine
from tests.object_support import STACK_LABEL, add_container, rows_of, volume_mount
from tests.services.conftest import ContainerRig

STACK = "shop"
FIRST, SECOND, THIRD, FOURTH, FIFTH = 1, 2, 3, 4, 5
RACE_NAMES = ("kept", "late", "vanishing", "raced", "replaced")


@pytest.fixture
def backfill(rig: ContainerRig) -> VolumeBackfillService:
    return build_volume_backfill(
        db=rig.db,
        clock=rig.runtime.clock,
        authoriser=rig.access.authoriser,
        inspector=rig.inspector,
        locks=rig.locks,
        controls=rig.access.controls,
        activity=rig.recorder,
    )


async def own(rig: ContainerRig, kind: ResourceType, resource_id: str, owner: int | None) -> None:
    await ResourceControlRepository(rig.db, rig.runtime.clock).create(
        ENV_A,
        kind,
        resource_id,
        is_public=False,
        is_administrators_only=False,
        access_level="write",
        owner_user_id=owner,
    )


async def created_by(
    rig: ContainerRig,
    number: int,
    user_id: int,
    *,
    action: str = "container_created",
    result: Outcome = "success",
) -> None:
    await rig.recorder.record_event(
        UserActor(user_id),
        action=action,
        target_type="container",
        target_id=container_id(number),
        environment_id=ENV_A,
        result=result,
    )


def outcomes(plan: BackfillPlan) -> dict[str, str]:
    return {volume.name: volume.outcome for volume in plan.volumes}


async def owner_of(rig: ContainerRig, name: str) -> int | str | None:
    control = await rig.access.controls.get(ADMIN, volume_key(name))
    return "uncontrolled" if control is None else control.owner_user_id


@pytest.fixture
async def catalogue(rig: ContainerRig, host: ContainerEngine) -> ContainerEngine:
    """One volume of each outcome, one inheriting a stack control and one controlled already."""
    for name in ("solo", "shared", "empty", "half", "owned", "_odd name"):
        host.add_volume(name)
    host.add_volume("stacked", labels={STACK_LABEL: STACK})
    add_container(host, FIRST, "web", mounts=[volume_mount("solo"), volume_mount("shared")])
    add_container(host, SECOND, "worker", mounts=[volume_mount("solo"), volume_mount("half")])
    add_container(host, THIRD, "db", mounts=[volume_mount("shared")])
    add_container(host, FOURTH, "stray", mounts=[volume_mount("half"), volume_mount("stacked")])
    await own(rig, "container", container_id(FIRST), STANDARD_ID)
    await own(rig, "container", container_id(SECOND), STANDARD_ID)
    await own(rig, "container", container_id(THIRD), MEMBER_ID)
    await own(rig, "stack", STACK, None)
    await own(rig, "volume", "owned", MEMBER_ID)
    return host


async def test_the_preview_assigns_only_a_volume_with_exactly_one_owner_across_its_containers(
    rig: ContainerRig, backfill: VolumeBackfillService, catalogue: ContainerEngine
) -> None:
    plan = await backfill.preview(ADMIN, ENV_A)

    by_name = {volume.name: volume for volume in plan.volumes}
    assert outcomes(plan) == {
        "_odd name": "unreadable",
        "empty": "no_candidate",
        "half": "no_candidate",
        "shared": "ambiguous",
        "solo": "assign",
    }
    assert [c.username for c in by_name["shared"].candidates] == [
        MEMBER_USERNAME,
        STANDARD_USERNAME,
    ]
    assert by_name["solo"].owner is not None and by_name["solo"].owner.user_id == STANDARD_ID
    assert by_name["solo"].source == "container_control"
    assert [c.name for c in by_name["solo"].containers] == ["web", "worker"]
    assert not plan.truncated and plan.uncontrolled_total == len(plan.volumes)
    assert await backfill.has_uncontrolled(ADMIN, ENV_A)
    with pytest.raises(PermissionDenied):
        await backfill.preview(STANDARD, ENV_A)


async def test_a_container_without_a_control_falls_back_to_the_newest_successful_creator(
    rig: ContainerRig, backfill: VolumeBackfillService, host: ContainerEngine
) -> None:
    host.add_volume("data")
    add_container(host, FIRST, "web", mounts=[volume_mount("data")])
    await created_by(rig, FIRST, STANDARD_ID)
    await created_by(rig, FIRST, MEMBER_ID, result="failure")

    plan = await backfill.preview(ADMIN, ENV_A)

    (volume,) = plan.volumes
    assert volume.outcome == "assign" and volume.source == "activity"
    assert volume.owner is not None and volume.owner.user_id == STANDARD_ID

    await created_by(rig, FIRST, MEMBER_ID)
    newer = await backfill.preview(ADMIN, ENV_A)

    assert newer.volumes[0].owner is not None and newer.volumes[0].owner.user_id == MEMBER_ID


async def test_a_recreate_row_does_not_name_a_creator_of_the_container(
    rig: ContainerRig, backfill: VolumeBackfillService, host: ContainerEngine
) -> None:
    host.add_volume("data")
    add_container(host, FIRST, "web", mounts=[volume_mount("data")])
    await created_by(rig, FIRST, STANDARD_ID, action="container_recreated")

    plan = await backfill.preview(ADMIN, ENV_A)

    assert outcomes(plan) == {"data": "no_candidate"}


async def test_a_creator_older_than_the_removal_of_the_containers_control_is_not_an_owner(
    rig: ContainerRig, backfill: VolumeBackfillService, host: ContainerEngine
) -> None:
    host.add_volume("data")
    add_container(host, FIRST, "web", mounts=[volume_mount("data")])
    await created_by(rig, FIRST, STANDARD_ID)
    await created_by(rig, FIRST, ADMIN.user_id, action="resource_control_deleted")

    removed = await backfill.preview(ADMIN, ENV_A)
    await created_by(rig, FIRST, MEMBER_ID)
    created_again = await backfill.preview(ADMIN, ENV_A)

    assert outcomes(removed) == {"data": "no_candidate"}
    assert created_again.volumes[0].owner is not None
    assert created_again.volumes[0].owner.user_id == MEMBER_ID


async def test_apply_writes_one_audited_control_per_assignment_and_none_for_the_rest(
    rig: ContainerRig, backfill: VolumeBackfillService, catalogue: ContainerEngine
) -> None:
    plan = await backfill.preview(ADMIN, ENV_A)

    result = await backfill.apply(ADMIN, ENV_A, plan.digest)

    assert [(item.name, item.status) for item in result.items] == [("solo", "assigned")]
    assert await owner_of(rig, "solo") == STANDARD_ID
    for untouched in ("shared", "empty", "half", "_odd name"):
        assert await owner_of(rig, untouched) == "uncontrolled"
    rows = await rows_of(rig.db, "resource_control_created")
    assert [(r["target_type"], r["target_id"], r["result"]) for r in rows] == [
        ("volume", "solo", "success")
    ]
    assert rows[0]["actor_user_id"] == ADMIN.user_id
    assert rows[0]["detail"]["backfill"] is True
    assert rows[0]["detail"]["candidate_source"] == "container_control"
    assert rows[0]["detail"]["containers"] == (
        f"{container_id(FIRST)[:12]},{container_id(SECOND)[:12]}"
    )
    assert (await backfill.preview(ADMIN, ENV_A)).of("assign") == ()


async def test_a_plan_that_changed_since_the_review_is_refused_before_any_write(
    rig: ContainerRig, backfill: VolumeBackfillService, catalogue: ContainerEngine
) -> None:
    plan = await backfill.preview(ADMIN, ENV_A)
    await own(rig, "container", container_id(FIFTH), MEMBER_ID)
    add_container(catalogue, FIFTH, "late", mounts=[volume_mount("solo")])

    with pytest.raises(BackfillChanged):
        await backfill.apply(ADMIN, ENV_A, plan.digest)
    with pytest.raises(BackfillChanged):
        await backfill.apply(ADMIN, ENV_A, "")

    assert await owner_of(rig, "solo") == "uncontrolled"
    assert await rows_of(rig.db, "resource_control_created") == []


async def test_a_volume_that_changes_after_the_recomputation_is_skipped_and_the_rest_proceed(
    rig: ContainerRig,
    backfill: VolumeBackfillService,
    host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for number, name in enumerate(RACE_NAMES, start=0x10):
        host.add_volume(name)
        add_container(host, number, f"user-of-{name}", mounts=[volume_mount(name)])
        await own(rig, "container", container_id(number), STANDARD_ID)
    plan = await backfill.preview(ADMIN, ENV_A)
    assert [v.name for v in plan.of("assign")] == ["kept", "late", "raced", "replaced", "vanishing"]
    reviewed = backfill._preview
    claim = rig.access.controls.claim

    async def preview_then_race(*args: Any) -> Any:
        reviewed_plan = await reviewed(*args)
        await own(rig, "volume", "late", MEMBER_ID)
        del host.volumes["vanishing"]
        host.volume_created["replaced"] = "2031-01-01T00:00:00Z"
        return reviewed_plan

    async def claim_after_a_rival(key: Any, owner: int | None, **options: Any) -> bool:
        if key.resource_id == "raced":
            await own(rig, "volume", "raced", MEMBER_ID)
        return await claim(key, owner, **options)

    monkeypatch.setattr(backfill, "_preview", preview_then_race)
    monkeypatch.setattr(rig.access.controls, "claim", claim_after_a_rival)

    result = await backfill.apply(ADMIN, ENV_A, plan.digest)

    assert [(item.name, item.status) for item in result.items] == [
        ("kept", "assigned"),
        ("late", "controlled"),
        ("raced", "controlled"),
        ("replaced", "changed"),
        ("vanishing", "gone"),
    ]
    assert await owner_of(rig, "kept") == STANDARD_ID
    assert await owner_of(rig, "late") == MEMBER_ID
    assert await owner_of(rig, "raced") == MEMBER_ID
    assert await owner_of(rig, "replaced") == "uncontrolled"
    rows = await rows_of(rig.db, "resource_control_created")
    assert [r["target_id"] for r in rows] == ["kept"]


async def test_apply_lists_the_containers_once_and_inspects_each_volume_under_its_lock(
    rig: ContainerRig, backfill: VolumeBackfillService, host: ContainerEngine
) -> None:
    for number, name in enumerate(("one", "two", "three"), start=0x10):
        host.add_volume(name)
        add_container(host, number, f"user-of-{name}", mounts=[volume_mount(name)])
        await own(rig, "container", container_id(number), STANDARD_ID)
    plan = await backfill.preview(ADMIN, ENV_A)
    before = len(host.requests)

    result = await backfill.apply(ADMIN, ENV_A, plan.digest)

    during = [f"{r.method} {r.path}" for r in host.requests[before:]]
    assert [item.status for item in result.items] == ["assigned"] * 3
    assert sum("/containers/json" in call for call in during) == 1
    inspected = [call.rsplit("/", 1)[-1] for call in during if "/volumes/" in call]
    assert inspected == ["one", "three", "two"]
