"""Volumes Docker makes or finds while a container is created: who owns them and what is undone."""

import asyncio
from typing import Any

import pytest

from app.db.repositories.resource_control import ResourceControlRepository
from app.services.activity import UserActor
from app.services.assembly import VolumeServices
from app.services.container_provisioning import VolumeChanged
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, control, volume_key
from tests.docker_engine.container_engine import ContainerEngine, anonymous_volume
from tests.object_support import rows_of
from tests.provisioning_support import IMAGE, create_form
from tests.services.conftest import ContainerRig

BACKED = {"o": "bind", "device": "/etc", "type": "none"}


def mounts(*sources: str) -> dict[str, str]:
    """A create form mounting each named volume at /v0, /v1, ...; an empty name is anonymous."""
    fields: dict[str, str] = {}
    for index, source in enumerate(sources):
        fields[f"mount-{index}-type"] = "volume"
        fields[f"mount-{index}-source"] = source
        fields[f"mount-{index}-target"] = f"/v{index}"
    return fields


def form(*sources: str) -> dict[str, str]:
    return create_form(**mounts(*sources))


def controls(rig: ContainerRig) -> ResourceControlRepository:
    return ResourceControlRepository(rig.db, rig.runtime.clock)


async def owner_of(rig: ContainerRig, name: str) -> int | None:
    held = await rig.access.controls.get(ADMIN, volume_key(name))
    return None if held is None else held.owner_user_id


async def created_row(rig: ContainerRig) -> dict[str, Any]:
    return (await rows_of(rig.db, "container_created"))[-1]


@pytest.fixture
def stocked(rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
    host.add_image(IMAGE)
    return rig


async def test_a_standard_users_create_claims_the_named_and_anonymous_volumes_docker_made(
    stocked: ContainerRig, vols: VolumeServices
) -> None:
    await stocked.services.creation.create(STANDARD, ENV_A, form("fresh", ""))

    created = await created_row(stocked)
    related = await rows_of(stocked.db, "volume_created")
    assert created["detail"]["volumes_claimed"] == 2
    assert {r["target_id"] for r in related} == {"fresh", anonymous_volume(1)}
    assert {r["detail"]["anonymous"] for r in related} == {True, False}
    assert all(r["detail"]["via_container"] and r["detail"]["control_created"] for r in related)
    assert await owner_of(stocked, "fresh") == STANDARD.user_id
    assert await owner_of(stocked, anonymous_volume(1)) == STANDARD.user_id
    listed = [row.name for row in (await vols.queries.page(STANDARD, ENV_A)).page.items]
    assert listed == [anonymous_volume(1), "fresh"]


async def test_an_administrators_create_claims_the_volumes_docker_made_too(
    stocked: ContainerRig,
) -> None:
    await stocked.services.creation.create(ADMIN, ENV_A, form("fresh"))

    assert (await created_row(stocked))["detail"]["volumes_claimed"] == 1
    assert await owner_of(stocked, "fresh") == ADMIN.user_id


async def test_a_volume_that_existed_before_the_create_is_mounted_but_not_claimed(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("old")
    await control(stocked.access, volume_key("old"), public=True, level="write")

    await stocked.services.creation.create(STANDARD, ENV_A, form("old"))

    detail = (await created_row(stocked))["detail"]
    assert "volumes_claimed" not in detail and "volumes_preexisting" not in detail
    assert await owner_of(stocked, "old") is None


async def test_a_create_that_makes_no_volume_records_no_claim(stocked: ContainerRig) -> None:
    await stocked.services.creation.create(STANDARD, ENV_A, create_form())

    assert "volumes_claimed" not in (await created_row(stocked))["detail"]
    assert await rows_of(stocked.db, "volume_created") == []


async def test_a_volume_that_appears_labelled_during_the_create_is_left_unclaimed_and_counted(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.on("create", lambda: host.add_volume("raced", labels={"owner": "someone"}))

    await stocked.services.creation.create(STANDARD, ENV_A, form("raced"))

    detail = (await created_row(stocked))["detail"]
    assert detail["volumes_preexisting"] == 1 and "volumes_claimed" not in detail
    assert await owner_of(stocked, "raced") is None


async def test_a_standard_users_container_is_undone_when_its_volume_turns_out_host_backed(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.on("create", lambda: host.add_volume("trap", options=BACKED))

    with pytest.raises(VolumeChanged) as refused:
        await stocked.services.creation.create(STANDARD, ENV_A, form("trap"))

    assert refused.value.rolled_back
    assert host.boxes == {} and "start" not in host.verbs()
    detail = (await created_row(stocked))["detail"]
    assert (detail["reason"], detail["rolled_back"]) == ("volume_changed", True)
    assert await owner_of(stocked, "trap") is None


async def test_an_administrators_host_backed_volume_is_left_unclaimed_without_undoing_the_create(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    host.on("create", lambda: host.add_volume("trap", options=BACKED))

    await stocked.services.creation.create(ADMIN, ENV_A, form("trap"))

    detail = (await created_row(stocked))["detail"]
    assert detail["volumes_preexisting"] == 1 and len(host.boxes) == 1
    assert await owner_of(stocked, "trap") is None


async def plant_in_pull(rig: ContainerRig, host: ContainerEngine, name: str, owner: int) -> None:
    """Make the volume and a control over it appear while the image is being pulled."""

    async def appear() -> None:
        host.add_volume(name)
        await controls(rig).create(
            ENV_A,
            "volume",
            name,
            is_public=False,
            is_administrators_only=False,
            access_level="write",
            owner_user_id=owner,
        )

    host.on("pull", appear)


async def test_a_volume_controlled_by_someone_else_at_create_undoes_a_standard_users_container(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    await plant_in_pull(rig, host, "raced", PEER.user_id)

    with pytest.raises(VolumeChanged) as refused:
        await rig.services.creation.create(STANDARD, ENV_A, form("raced"))

    assert refused.value.rolled_back and host.boxes == {}
    assert await owner_of(rig, "raced") == PEER.user_id


async def test_the_same_volume_for_an_administrator_is_mounted_and_counted_as_preexisting(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    await plant_in_pull(rig, host, "raced", PEER.user_id)

    await rig.services.creation.create(ADMIN, ENV_A, form("raced"))

    detail = (await created_row(rig))["detail"]
    assert detail["volumes_preexisting"] == 1 and "volumes_claimed" not in detail
    assert await owner_of(rig, "raced") == PEER.user_id


async def test_a_present_volume_whose_access_is_withdrawn_during_the_pull_undoes_the_create(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), level="write", owner=STANDARD.user_id)

    async def withdraw() -> None:
        found = await controls(rig).get_for(ENV_A, "volume", "shared")
        assert found is not None
        await controls(rig).update(
            found.id,
            is_public=False,
            is_administrators_only=True,
            access_level="write",
            owner_user_id=None,
        )

    host.on("pull", withdraw)

    with pytest.raises(VolumeChanged):
        await rig.services.creation.create(STANDARD, ENV_A, form("shared"))

    assert host.boxes == {}


async def test_a_present_volume_swapped_for_a_host_backed_one_undoes_the_create(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), level="write", owner=STANDARD.user_id)
    host.on("pull", lambda: host.add_volume("shared", options=BACKED))

    with pytest.raises(VolumeChanged):
        await rig.services.creation.create(STANDARD, ENV_A, form("shared"))

    assert host.boxes == {}


async def test_a_present_volume_recreated_with_a_new_creation_time_undoes_the_create(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), level="write", owner=STANDARD.user_id)
    host.on("pull", lambda: host.add_volume("shared", created="2025-01-01T00:00:00Z"))

    with pytest.raises(VolumeChanged):
        await rig.services.creation.create(STANDARD, ENV_A, form("shared"))

    assert host.boxes == {}


async def test_a_present_volume_that_is_unchanged_is_kept_and_mounted(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    host.add_volume("shared")
    await control(rig.access, volume_key("shared"), level="write", owner=STANDARD.user_id)

    outcome = await rig.services.creation.create(STANDARD, ENV_A, form("shared"))

    assert outcome.container_id in host.boxes


async def test_a_control_left_by_an_earlier_volume_of_the_name_is_replaced_and_recorded(
    stocked: ContainerRig,
) -> None:
    await control(stocked.access, volume_key("db-data"), level="write", owner=PEER.user_id)

    await stocked.services.creation.create(STANDARD, ENV_A, form("db-data"))

    assert await owner_of(stocked, "db-data") == STANDARD.user_id
    (related,) = await rows_of(stocked.db, "volume_created")
    assert related["detail"]["stale_control_replaced"] is True


async def test_an_absent_volume_is_locked_from_the_reach_check_until_the_create_is_done(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    key = ObjectKey(ENV_A, "volume", "lockedvol")
    other = LockHolder(UserActor(PEER.user_id), "peer", "create")
    seen: list[str] = []

    async def probe() -> None:
        try:
            async with rig.engine.locks.hold(key, other):
                seen.append("free")
        except ObjectBusy:
            seen.append("busy")

    host.on("pull", probe)

    await rig.services.creation.create(STANDARD, ENV_A, form("lockedvol"))

    assert seen == ["busy"]
    assert rig.engine.locks.holder_of(key) is None


async def test_a_network_named_like_a_network_id_is_found_by_name(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    name = "frontendbackendnetwork123"
    host.add_network(name, 0x77)

    outcome = await stocked.services.creation.create(
        ADMIN, ENV_A, create_form(network_mode="networks", **{"network-0-name": name})
    )

    assert outcome.container_id in host.boxes


async def test_an_interrupted_create_whose_rollback_failed_records_a_failure_and_claims_nothing(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    holder: dict[str, asyncio.Future[Any]] = {}

    async def cancel_after_the_volume_turns_host_backed() -> None:
        host.add_volume("trap", options=BACKED)
        holder["task"].cancel()
        await asyncio.sleep(0)

    host.on("create", cancel_after_the_volume_turns_host_backed)
    host.fail("remove", 500)
    holder["task"] = asyncio.ensure_future(
        stocked.services.creation.create(STANDARD, ENV_A, form("trap"))
    )

    with pytest.raises(asyncio.CancelledError):
        await holder["task"]

    rows = await rows_of(stocked.db, "container_created")
    assert [r["result"] for r in rows] == ["failure"]
    assert rows[0]["detail"]["reason"] == "volume_changed"
    assert "volumes_claimed" not in rows[0]["detail"]
    assert await owner_of(stocked, "trap") is None


async def test_a_create_cancelled_after_the_scan_still_claims_its_volumes(
    stocked: ContainerRig, host: ContainerEngine
) -> None:
    gate = host.hold("start")
    task = asyncio.ensure_future(stocked.services.creation.create(STANDARD, ENV_A, form("fresh")))
    await gate.entered.wait()
    task.cancel()
    gate.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    created = await created_row(stocked)
    assert created["detail"]["volumes_claimed"] == 1
    assert [r["target_id"] for r in await rows_of(stocked.db, "volume_created")] == ["fresh"]
    assert await owner_of(stocked, "fresh") == STANDARD.user_id
