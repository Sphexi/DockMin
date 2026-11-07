"""Swarm configs: create, read, remove, controls and the reference counting that guards removal."""

import asyncio
import base64

import pytest

import app.services.swarm_configs as configs_module
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services.access import ResourceKey
from app.services.container_actions import EngineRefused
from app.services.swarm_configs import (
    SwarmObjectChangedSinceReview,
    SwarmObjectGone,
    SwarmObjectInUse,
    SwarmObjectInvalid,
    SwarmObjectNameInUse,
    SwarmObjectRefused,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, PEER, STANDARD, control
from tests.object_support import drain_background, rows_of
from tests.services.conftest import SwarmRig
from tests.swarm_support import MISSING_ID, NAMESPACE_LABEL

MAX_CONFIG_BYTES = 128 * 1024
SETTLE_SECONDS = 0.05


def form(name: str, **fields: str) -> dict[str, str]:
    return {"name": name, **fields}


async def make(rig: SwarmRig, name: str = "app-conf", content: str = "a=1") -> str:
    created = await rig.sw.config_actions.create(ADMIN, ENV_A, form(name, content=content))
    rig.fresh()
    return created.id


async def test_creating_a_config_stores_its_content_claims_the_control_and_records_a_row(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.config_actions.create(
        ADMIN,
        ENV_A,
        form("app-conf", content="a=1\r\nb=2", **{"label-0-key": "tier", "label-0-value": "web"}),
    )

    [row] = await rows_of(swarm.db, "config_created")
    view = await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "config", created.id))
    assert row["detail"] == {"name": "app-conf", "size": 7, "labels": 1, "control_created": True}
    assert (row["target_type"], row["target_id"]) == ("config", created.id)
    assert swarm.host.configs[created.id]["Spec"]["Data"] == base64.b64encode(b"a=1\nb=2").decode()
    assert view is not None and view.owner_user_id == ADMIN.user_id


async def test_a_config_shows_its_content_labels_and_row_in_the_list(swarm: SwarmRig) -> None:
    config_id = await swarm.sw.config_actions.create(
        ADMIN,
        ENV_A,
        form("app-conf", content="a=1\nb=2", **{"label-0-key": "tier", "label-0-value": "web"}),
    )
    swarm.fresh()

    detail = await swarm.sw.configs.detail(ADMIN, ENV_A, config_id.id)
    page = await swarm.sw.configs.page(ADMIN, ENV_A)

    assert detail.content.text == "a=1\nb=2" and detail.content.size == 7
    assert [label.key for label in detail.labels] == ["tier"]
    assert [r.name for r in page.page.items] == ["app-conf"] and page.total == 1


@pytest.mark.parametrize(
    ("fields", "expected", "too_large"),
    [
        pytest.param({"name": "", "content": "x"}, {"name"}, False, id="no name"),
        pytest.param({"name": "-bad", "content": "x"}, {"name"}, False, id="bad name"),
        pytest.param({"name": "c" * 25, "content": "x"}, {"name"}, False, id="id-shaped name"),
        pytest.param({"name": "ok", "content": ""}, {"content"}, False, id="empty content"),
        pytest.param(
            {"name": "ok", "content": "x" * (MAX_CONFIG_BYTES + 1)},
            {"content"},
            True,
            id="over the cap",
        ),
        pytest.param(
            {
                "name": "ok",
                "content": "x",
                "label-0-key": NAMESPACE_LABEL,
                "label-0-value": "a",
            },
            {"labels"},
            False,
            id="reserved label",
        ),
        pytest.param(
            {"name": "ok", "content": "x", "label-0-key": "k\x01"},
            {"label-0-key"},
            False,
            id="control character in a label key",
        ),
    ],
)
async def test_a_refused_create_writes_a_failure_row_and_never_reaches_the_engine(
    swarm: SwarmRig, fields: dict[str, str], expected: set[str], too_large: bool
) -> None:
    with pytest.raises(SwarmObjectInvalid) as raised:
        await swarm.sw.config_actions.create(ADMIN, ENV_A, fields)

    [row] = await rows_of(swarm.db, "config_created")
    assert set(raised.value.fields) == expected and raised.value.too_large is too_large
    assert (row["result"], row["detail"]["reason"]) == (
        "failure",
        "too_large" if too_large else "invalid",
    )
    assert swarm.host.create_bodies == []


async def test_engine_refusals_on_create_map_to_fixed_reasons(swarm: SwarmRig) -> None:
    swarm.host.add_config("taken")

    with pytest.raises(SwarmObjectNameInUse):
        await swarm.sw.config_actions.create(ADMIN, ENV_A, form("taken", content="x"))
    swarm.host.create_status = 400
    with pytest.raises(SwarmObjectRefused):
        await swarm.sw.config_actions.create(ADMIN, ENV_A, form("fresh", content="x"))
    swarm.host.create_status = 503
    with pytest.raises(Exception) as unavailable:
        await swarm.sw.config_actions.create(ADMIN, ENV_A, form("fresh", content="x"))

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "config_created")]
    assert reasons == ["name_in_use", "engine_rejected", "not_swarm_manager"]
    assert type(unavailable.value).__name__ == "SwarmUnavailable"
    assert swarm.poker.poked == [ENV_A]


async def test_a_config_lookup_that_fails_is_an_outage_not_a_missing_object(
    swarm: SwarmRig,
) -> None:
    swarm.host.outage = {"configs"}

    with pytest.raises(Exception) as raised:
        await swarm.sw.configs.page(ADMIN, ENV_A)

    assert type(raised.value).__name__ == "SwarmUnavailable"


@pytest.mark.parametrize(
    ("content", "text"),
    [
        pytest.param(b"\x00\x01\x02", None, id="binary"),
        pytest.param("naïve\n".encode(), "naïve\n", id="utf-8"),
    ],
)
async def test_content_is_shown_as_text_only_when_it_is_text(
    swarm: SwarmRig, content: bytes, text: str | None
) -> None:
    config_id = swarm.host.add_config("c", content)

    shown = (await swarm.sw.configs.detail(ADMIN, ENV_A, config_id)).content

    assert (shown.text, shown.size, shown.truncated) == (text, len(content), False)


async def test_content_over_the_display_limit_is_cut_but_its_size_is_reported_whole(
    swarm: SwarmRig,
) -> None:
    config_id = swarm.host.add_config("big", ("é" * 40000).encode())

    shown = (await swarm.sw.configs.detail(ADMIN, ENV_A, config_id)).content

    assert shown.truncated and shown.size == 80000
    assert shown.text is not None and len(shown.text.encode()) <= 64 * 1024


async def test_removing_a_config_releases_its_control_and_deletes_it(swarm: SwarmRig) -> None:
    config_id = await make(swarm)
    key = ResourceKey(ENV_A, "config", config_id)

    impact = await swarm.sw.configs.remove_impact(ADMIN, ENV_A, config_id)
    removed = await swarm.sw.config_actions.remove(
        ADMIN, ENV_A, config_id, expected_name="app-conf", expected_in_use=0
    )

    [gone] = await rows_of(swarm.db, "config_removed")
    assert impact.in_use == 0 and impact.removable and removed.control_released
    assert gone["detail"] == {"name": "app-conf", "control_released": True}
    assert config_id not in swarm.host.configs
    assert await swarm.access.controls.get(ADMIN, key) is None


async def test_a_config_a_service_references_is_refused_naming_the_service_before_the_engine(
    swarm: SwarmRig,
) -> None:
    config_id = swarm.host.add_config("conf")
    swarm.host.named_service("web", configs=[(config_id, "conf")])

    impact = await swarm.sw.configs.remove_impact(ADMIN, ENV_A, config_id)
    detail = await swarm.sw.configs.detail(ADMIN, ENV_A, config_id)
    with pytest.raises(SwarmObjectChangedSinceReview):
        await swarm.sw.config_actions.remove(
            ADMIN, ENV_A, config_id, expected_name="conf", expected_in_use=0
        )
    with pytest.raises(SwarmObjectInUse) as raised:
        await swarm.sw.config_actions.remove(
            ADMIN, ENV_A, config_id, expected_name="conf", expected_in_use=1
        )

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "config_removed")]
    assert impact.in_use == 1 and not impact.removable
    assert [u.name for u in impact.services] == ["web"] and detail.in_use == 1
    assert [u.name for u in raised.value.users] == ["web"]
    assert reasons == ["changed_since_review", "config_in_use"]
    assert config_id in swarm.host.configs and not swarm.host.calls("DELETE")


async def test_a_name_that_changed_since_the_review_is_refused(swarm: SwarmRig) -> None:
    config_id = await make(swarm)

    with pytest.raises(SwarmObjectChangedSinceReview):
        await swarm.sw.config_actions.remove(
            ADMIN, ENV_A, config_id, expected_name="other", expected_in_use=0
        )

    assert config_id in swarm.host.configs


async def test_an_engine_refusal_without_a_service_using_it_is_engine_rejected(
    swarm: SwarmRig,
) -> None:
    config_id = await make(swarm, "c1", "x")
    swarm.host.remove_status = 400

    with pytest.raises(EngineRefused):
        await swarm.sw.config_actions.remove(
            ADMIN, ENV_A, config_id, expected_name="c1", expected_in_use=0
        )

    assert (await rows_of(swarm.db, "config_removed"))[-1]["detail"]["reason"] == "engine_rejected"


async def test_a_config_gone_from_the_host_releases_the_control_and_says_gone(
    swarm: SwarmRig,
) -> None:
    config_id = await make(swarm, "c1", "x")
    swarm.host.remove_status = 404

    with pytest.raises(SwarmObjectGone):
        await swarm.sw.config_actions.remove(
            ADMIN, ENV_A, config_id, expected_name="c1", expected_in_use=0
        )

    assert await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "config", config_id)) is None


async def test_a_standard_user_sees_and_removes_only_their_own_and_every_other_id_reads_alike(
    swarm: SwarmRig,
) -> None:
    mine = await swarm.sw.config_actions.create(STANDARD, ENV_A, form("mine", content="x"))
    other = swarm.host.add_config("admins-only")
    swarm.fresh()

    mine_page = await swarm.sw.configs.page(STANDARD, ENV_A)
    peer_page = await swarm.sw.configs.page(PEER, ENV_A)
    admin_page = await swarm.sw.configs.page(ADMIN, ENV_A)
    messages = set()
    for candidate in (other, MISSING_ID, "not-an-id", "A" * 25):
        with pytest.raises(ResourceNotFound) as raised:
            await swarm.sw.configs.detail(STANDARD, ENV_A, candidate)
        messages.add(str(raised.value))
    with pytest.raises(ResourceNotFound):
        await swarm.sw.configs.detail(PEER, ENV_A, mine.id)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.config_actions.remove(
            PEER, ENV_A, mine.id, expected_name="mine", expected_in_use=0
        )
    await swarm.sw.config_actions.remove(
        STANDARD, ENV_A, mine.id, expected_name="mine", expected_in_use=0
    )

    assert mine.control_created
    assert [r.name for r in mine_page.page.items] == ["mine"] and peer_page.page.items == []
    assert [r.name for r in admin_page.page.items] == ["admins-only", "mine"]
    assert len(messages) == 1


async def test_the_access_panel_is_for_administrators_even_on_ones_own_config(
    swarm: SwarmRig,
) -> None:
    mine = await swarm.sw.config_actions.create(STANDARD, ENV_A, form("mine", content="x"))
    swarm.fresh()

    with pytest.raises(PermissionDenied):
        await swarm.sw.configs.access_panel(STANDARD, ENV_A, mine.id)
    panel = await swarm.sw.configs.access_panel(ADMIN, ENV_A, mine.id)

    assert panel.key == ResourceKey(ENV_A, "config", mine.id) and panel.control is not None


async def test_on_a_local_socket_a_standard_user_reads_a_granted_config_but_cannot_write(
    swarm: SwarmRig,
) -> None:
    config_id = swarm.host.add_config("b-visible")
    await control(swarm.access, ResourceKey(ENV_B, "config", config_id), users=(STANDARD.user_id,))
    swarm.fresh()

    detail = await swarm.sw.configs.detail(STANDARD, ENV_B, config_id)
    with pytest.raises(PermissionDenied):
        await swarm.sw.config_actions.create(STANDARD, ENV_B, form("local", content="x"))
    with pytest.raises(PermissionDenied):
        await swarm.sw.config_actions.remove(
            STANDARD, ENV_B, config_id, expected_name="b-visible", expected_in_use=0
        )

    assert detail.name == "b-visible"
    assert [r for r in await rows_of(swarm.db) if r["result"] == "failure"] == []


async def test_a_create_cancelled_after_dispatch_still_writes_its_row_and_claims(
    swarm: SwarmRig,
) -> None:
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(swarm.sw.config_actions.create(ADMIN, ENV_A, form("c", content="x")))
    await asyncio.wait_for(swarm.host.arrived.wait(), 2)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [row] = await rows_of(swarm.db, "config_created")
    [config_id] = list(swarm.host.configs)
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "config", config_id))


async def test_a_create_still_unfinished_is_outcome_unknown_then_settles_late(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(configs_module, "SETTLE_SECONDS", SETTLE_SECONDS)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(
        swarm.sw.config_actions.create(ADMIN, ENV_A, form("slow", content="x"))
    )
    await asyncio.wait_for(swarm.host.arrived.wait(), 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    first = await rows_of(swarm.db, "config_created")
    swarm.host.gate.set()
    await drain_background()

    rows = await rows_of(swarm.db, "config_created")
    assert [r["result"] for r in first] == ["failure"]
    assert first[0]["detail"]["reason"] == "outcome_unknown"
    assert [r["result"] for r in rows] == ["failure", "success"]
    assert rows[1]["detail"]["settled_late"] is True and rows[1]["detail"]["control_created"]
