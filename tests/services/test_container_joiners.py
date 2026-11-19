"""A recreate a user starts: the chain it names, follows and reports."""

from typing import Any

import pytest

from app.services.access import ResourceKey
from app.services.network_joiners import MAX_JOINER_CHAIN
from tests.access_support import ADMIN, ENV_A, STANDARD, container_id, control
from tests.access_support import container_key as container_key_of
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ContainerEngine
from tests.environment_support import activity_rows
from tests.provisioning_support import IMAGE, OLD, old_body, seed_recreatable
from tests.services.conftest import ContainerRig

BY_ID = container_id(0xC2)
BY_NAME = container_id(0xC3)
STOPPED = container_id(0xC4)
GRANDCHILD = container_id(0xC5)
MANAGED = container_id(0xC6)
READ_ONLY = container_id(0xC7)
HIDDEN = container_id(0xC8)
LATE = container_id(0xC9)
PROJECT_LABEL = "com.docker.compose.project"
OLD_KEY = ResourceKey(ENV_A, "container", OLD)


def add_joiner(
    host: ContainerEngine,
    identifier: str,
    name: str,
    mode: str,
    state: str = "running",
    labels: dict[str, str] | None = None,
) -> None:
    network_id = next(iter(host.networks))
    body = old_body(
        network_id,
        config={"Image": IMAGE, "NetworkingConfig": {}, "Labels": labels or {}},
        host={"NetworkMode": mode},
    )
    host.add_configured(identifier, name, body, state)


def mode_of(host: ContainerEngine, container: str) -> str:
    return str(host.boxes[container].inspect()["HostConfig"]["NetworkMode"])


async def rows_of(rig: ContainerRig, action: str) -> dict[str, dict[str, Any]]:
    rows = await activity_rows(rig.db)
    return {row["target_id"]: row for row in rows if row["action"] == action}


@pytest.fixture
def chain(rig: ContainerRig, host: ContainerEngine) -> ContainerEngine:
    seed_recreatable(host)
    add_joiner(host, BY_ID, "by-id", f"container:{OLD}")
    add_joiner(host, BY_NAME, "by-name", "container:web")
    add_joiner(host, STOPPED, "idle", f"container:{OLD}", state="exited")
    add_joiner(host, GRANDCHILD, "sidecar", f"container:{BY_ID}")
    add_joiner(host, MANAGED, "managed", f"container:{OLD}", labels={PROJECT_LABEL: "shop"})
    return host


async def test_a_preview_names_each_joiner_with_what_will_be_done_to_it(
    rig: ContainerRig, chain: ContainerEngine
) -> None:
    preview = await rig.services.joiners.preview(ADMIN, ENV_A, OLD, "web")

    assert {(item.name, item.action) for item in preview.joiners} == {
        ("by-id", "recreate"),
        ("sidecar", "recreate"),
        ("idle", "recreate"),
        ("by-name", "restart"),
        ("managed", "blocked"),
    }
    assert set(preview.confirmed_ids) == {BY_ID, GRANDCHILD, STOPPED, BY_NAME}
    assert (preview.refused, preview.hidden, preview.omitted) == ((), 0, 0)


async def test_a_recreate_follows_the_chain_and_reports_each_joiner(
    rig: ContainerRig, chain: ContainerEngine
) -> None:
    preview = await rig.services.joiners.preview(ADMIN, ENV_A, OLD, "web")

    outcome, report = await rig.services.joiners.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", confirmed=preview.confirmed_ids
    )

    renewed = {box.name: box for box in chain.boxes.values()}
    assert sorted(report.recreated) == ["by-id", "idle", "sidecar"]
    assert report.restarted == ["by-name"]
    assert report.failed == [("managed", "compose_managed")]
    assert mode_of(chain, renewed["by-id"].id) == f"container:{outcome.new_id}"
    assert mode_of(chain, renewed["sidecar"].id) == f"container:{renewed['by-id'].id}"
    assert renewed["idle"].state != "running"
    recreated = await rows_of(rig, "container_recreated")
    assert recreated[GRANDCHILD]["detail"]["joined"] == "by-id"
    assert recreated[MANAGED]["detail"]["reason"] == "compose_managed"
    assert {row["actor_user_id"] for row in recreated.values()} == {1}
    assert recreated[OLD]["detail"]["joiners_recreate"] == "by-id, idle, managed"
    assert recreated[OLD]["detail"]["joiners_restart"] == "by-name"


async def test_a_joiner_that_appeared_after_the_confirmation_is_reported_and_left_alone(
    rig: ContainerRig, chain: ContainerEngine
) -> None:
    preview = await rig.services.joiners.preview(ADMIN, ENV_A, OLD, "web")
    add_joiner(chain, LATE, "late", f"container:{OLD}")
    add_joiner(chain, container_id(0xCA), "late-name", "container:web")

    _, report = await rig.services.joiners.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", confirmed=preview.confirmed_ids
    )

    assert LATE in chain.boxes
    assert ("late", "not_confirmed") in report.failed
    assert ("late-name", "not_confirmed") in report.failed
    failed = await rows_of(rig, "container_recreated")
    assert (failed[LATE]["result"], failed[LATE]["detail"]["reason"]) == (
        "failure",
        "not_confirmed",
    )
    assert (await rows_of(rig, "container_restarted"))[container_id(0xCA)]["result"] == "failure"


async def test_a_user_names_and_changes_only_what_the_user_may(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    add_joiner(host, BY_NAME, "by-name", "container:web")
    add_joiner(host, READ_ONLY, "read-only", "container:web")
    add_joiner(host, HIDDEN, "hidden", "container:web")
    async with rig.db.transaction():
        await rig.access.controls.claim(OLD_KEY, STANDARD_ID)
    await control(rig.access, container_key_of(0xC3), public=True, level="write")
    await control(rig.access, container_key_of(0xC7), public=True, level="read")
    await control(rig.access, container_key_of(0xC8), administrators_only=True)

    preview = await rig.services.joiners.preview(STANDARD, ENV_A, OLD, "web")
    _, report = await rig.services.joiners.recreate(
        STANDARD, ENV_A, OLD, expected_name="web", confirmed=preview.confirmed_ids
    )

    assert [item.name for item in preview.joiners] == ["by-name"]
    assert [item.name for item in preview.refused] == ["read-only"]
    assert preview.hidden == 1
    assert report.restarted == ["by-name"]
    assert report.failed == [("read-only", "permission_denied")]
    rows = await activity_rows(rig.db)
    assert "hidden" not in str([row["detail"] for row in rows if row["target_id"] != OLD])
    refused = next(
        r for r in rows if r["target_id"] == READ_ONLY and r["action"] == "container_restarted"
    )
    assert (refused["result"], refused["detail"]["reason"]) == ("failure", "permission_denied")
    owner_row = next(
        r for r in rows if r["target_id"] == OLD and r["action"] == "container_recreated"
    )
    assert owner_row["detail"]["joiners_hidden"] == 1
    assert owner_row["detail"]["joiners_refused"] == "read-only"


async def test_a_chain_past_the_limit_is_followed_to_the_limit_and_the_rest_are_counted(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    extra = 2
    for number in range(MAX_JOINER_CHAIN + extra):
        add_joiner(host, container_id(0x3000 + number), f"n{number:03d}", "container:web")

    preview = await rig.services.joiners.preview(ADMIN, ENV_A, OLD, "web")
    _, report = await rig.services.joiners.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", confirmed=None
    )

    assert (len(preview.joiners), preview.omitted) == (MAX_JOINER_CHAIN, extra)
    assert (len(report.restarted), report.omitted) == (MAX_JOINER_CHAIN, extra)


async def test_a_recreate_stands_automation_back_from_the_new_container_as_well_as_the_old(
    rig: ContainerRig, host: ContainerEngine
) -> None:
    seed_recreatable(host)
    noted: list[tuple[str, str]] = []
    rig.engine.locks.set_observer(lambda key, holder: noted.append((key.object_id, holder.action)))

    outcome, _ = await rig.services.joiners.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", confirmed=()
    )

    assert (OLD, "recreate") in noted
    assert (outcome.new_id, "recreate") in noted
