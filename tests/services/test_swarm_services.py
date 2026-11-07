"""Service create, update, scale, force update, rollback and remove over a fake manager."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import app.docker_engine.resources as engine_resources
import app.services.swarm_services as services_module
from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.object_inspector import (
    EnvironmentUnavailable,
    ObjectTooLarge,
    SwarmUnavailable,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.swarm_service_base import display_image, migrate_legacy_networks
from app.services.swarm_service_views import (
    NoPreviousSpec,
    NotReplicated,
    ServiceChangedSinceReview,
    ServiceFilters,
    ServiceGone,
    ServiceInvalid,
    ServiceNameInUse,
    ServiceRefused,
)
from tests.access_support import ADMIN, ENV_A, ENV_B, PEER, STANDARD, control
from tests.docker_engine.fake_engine import Stall
from tests.docker_engine.swarm_engine import DIGEST, MANAGER, WORKER, plain_spec
from tests.object_support import drain_background, rows_of
from tests.services.conftest import SwarmRig
from tests.swarm_support import MISSING_ID, NAMESPACE_LABEL

FORM = {
    "name": "web",
    "image": "busybox:latest",
    "mode": "replicated",
    "replicas": "2",
    "command": "sleep 100",
}
GLOBAL_FORM = {"name": "agent", "image": "busybox:latest", "mode": "global", "replicas": ""}
INTERRUPTED_SETTLE_SECONDS = 0.05
RUNNING_AFTER_ONE_STARTS_LATE = 1
DESIRED_REPLICAS = 2
EXPECTED_CREATE_DETAIL: dict[str, Any] = {
    "name": "web",
    "mode": "replicated",
    "replicas": 2,
    "image": f"busybox:latest@{DIGEST}",
    "digest_pinned": True,
    "networks": 0,
    "ports": 0,
    "mounts": 0,
    "configs": 0,
    "secrets": 0,
    "constraints": 1,
    "labels": 1,
    "pinned_to_manager": False,
    "credential_set_id": None,
    "anonymous": True,
    "credential_source": "no_match",
    "control_created": True,
    "binding": "created",
}


async def deploy(rig: SwarmRig, who: Any = ADMIN, **fields: str) -> str:
    created = await rig.sw.service_actions.create(who, ENV_A, {**FORM, **fields})
    rig.fresh()
    return created.service_id


async def version_of(rig: SwarmRig, service_id: str) -> int:
    detail = await rig.sw.services.detail(ADMIN, ENV_A, service_id)
    rig.fresh()
    return detail.version


async def last_row(rig: SwarmRig, action: str) -> dict[str, Any]:
    return (await rows_of(rig.db, action))[-1]


def stored(rig: SwarmRig, service_id: str) -> dict[str, Any]:
    returned: dict[str, Any] = rig.host.services[service_id]["Spec"]
    return returned


async def test_creating_a_service_sends_the_built_spec_pins_the_digest_and_claims_the_control(
    swarm: SwarmRig,
) -> None:
    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    spec = stored(swarm, created.service_id)
    key = ResourceKey(ENV_A, "service", created.service_id)
    assert spec["Name"] == "web" and spec["Mode"] == {"Replicated": {"Replicas": 2}}
    assert spec["TaskTemplate"]["ContainerSpec"]["Args"] == ["sleep", "100"]
    assert spec["TaskTemplate"]["ContainerSpec"]["Image"] == f"busybox:latest@{swarm.host.digest}"
    assert (created.digest_pinned, created.control_created, created.warning) == (True, True, None)
    view = await swarm.access.controls.get(ADMIN, key)
    assert view is not None and view.owner_user_id == ADMIN.user_id


async def test_a_created_service_is_recorded_with_counts_and_never_with_values(
    swarm: SwarmRig,
) -> None:
    form = {
        **FORM,
        "env-0-key": "DB_PASSWORD",
        "env-0-value": "hunter2-env",
        "label-0-key": "api_key",
        "label-0-value": "label-secret-value",
        "constraints": "node.role==worker",
    }

    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, form)

    [row] = await rows_of(swarm.db, "service_created")
    assert (row["result"], row["target_type"], row["target_id"]) == (
        "success",
        "service",
        created.service_id,
    )
    assert row["detail"] == EXPECTED_CREATE_DETAIL
    assert "hunter2-env" not in json.dumps(row) and "label-secret-value" not in json.dumps(row)


@pytest.mark.parametrize(
    ("form", "fields"),
    [
        pytest.param({**FORM, "name": "x y"}, {"name"}, id="name"),
        pytest.param({**FORM, "image": ""}, {"image"}, id="image"),
        pytest.param({**FORM, "replicas": "x"}, {"replicas"}, id="replicas"),
        pytest.param({**FORM, "name": "-", "image": ""}, {"name", "image"}, id="several"),
    ],
)
async def test_an_invalid_create_names_its_fields_writes_a_failure_row_and_sends_nothing(
    swarm: SwarmRig, form: dict[str, str], fields: set[str]
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await swarm.sw.service_actions.create(ADMIN, ENV_A, form)

    [row] = await rows_of(swarm.db, "service_created")
    assert set(raised.value.fields) == fields
    assert (row["result"], row["detail"]["reason"]) == ("failure", "invalid")
    assert swarm.host.create_bodies == [] and swarm.host.services == {}


async def test_a_duplicate_name_is_name_in_use_and_an_engine_refusal_is_engine_rejected(
    swarm: SwarmRig,
) -> None:
    await deploy(swarm)

    with pytest.raises(ServiceNameInUse):
        await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)
    swarm.host.create_status = 400
    with pytest.raises(ServiceRefused) as refused:
        await swarm.sw.service_actions.create(ADMIN, ENV_A, {**FORM, "name": "other"})

    reasons = [r["detail"]["reason"] for r in (await rows_of(swarm.db, "service_created"))[1:]]
    assert reasons == ["name_in_use", "engine_rejected"]
    assert "engine detail" not in str(refused.value)


async def test_a_manager_that_stopped_being_one_surfaces_as_swarm_unavailable_and_pokes(
    swarm: SwarmRig,
) -> None:
    swarm.host.create_status = 503

    with pytest.raises(SwarmUnavailable):
        await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    row = await last_row(swarm, "service_created")
    assert row["detail"]["reason"] == "not_swarm_manager" and swarm.poker.poked == [ENV_A]


async def test_a_config_deleted_between_the_check_and_the_create_is_a_field_error_not_a_refusal(
    swarm: SwarmRig,
) -> None:
    config_id = swarm.host.add_config("cfg")
    swarm.host.gate = asyncio.Event()
    form = {**FORM, "config-0-name": "cfg", "config-0-target": "/c"}
    task = asyncio.create_task(swarm.sw.service_actions.create(ADMIN, ENV_A, form))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    del swarm.host.configs[config_id]
    swarm.host.gate.set()
    with pytest.raises(ServiceInvalid) as raised:
        await task

    row = await last_row(swarm, "service_created")
    assert "configs" in raised.value.fields
    assert (row["result"], row["detail"]["reason"]) == ("failure", "config_gone")


async def test_scaling_records_the_replica_count_before_and_after_each_time(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)

    for replicas, expected in ((3, 2), (1, 3)):
        version = await version_of(swarm, service_id)
        scaled = await swarm.sw.service_actions.scale(
            ADMIN,
            ENV_A,
            service_id,
            replicas=replicas,
            expected_version=version,
            expected_replicas=expected,
        )
        swarm.fresh()
        assert (scaled.replicas_before, scaled.replicas_after) == (expected, replicas)

    rows = await rows_of(swarm.db, "service_scaled")
    assert [(r["detail"]["replicas_before"], r["detail"]["replicas_after"]) for r in rows] == [
        (2, 3),
        (3, 1),
    ]
    assert stored(swarm, service_id)["Mode"] == {"Replicated": {"Replicas": 1}}


async def test_scaling_with_a_reviewed_version_or_count_that_no_longer_holds_changes_nothing(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)

    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.scale(
            ADMIN, ENV_A, service_id, replicas=3, expected_version=version - 1, expected_replicas=2
        )
    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.scale(
            ADMIN, ENV_A, service_id, replicas=3, expected_version=version, expected_replicas=5
        )

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "service_scaled")]
    assert reasons == ["changed_since_review", "changed_since_review"]
    assert stored(swarm, service_id)["Mode"] == {"Replicated": {"Replicas": 2}}


async def test_scaling_a_global_service_is_refused_and_a_count_outside_the_range_is_invalid(
    swarm: SwarmRig,
) -> None:
    global_id = await deploy(swarm, **GLOBAL_FORM)
    version = await version_of(swarm, global_id)

    with pytest.raises(NotReplicated):
        await swarm.sw.service_actions.scale(
            ADMIN, ENV_A, global_id, replicas=2, expected_version=version, expected_replicas=1
        )
    with pytest.raises(ValidationFailed):
        await swarm.sw.service_actions.scale(
            ADMIN, ENV_A, global_id, replicas=1001, expected_version=version, expected_replicas=1
        )

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "service_scaled")]
    assert reasons == ["not_replicated", "invalid"]


async def test_a_force_update_bumps_the_counter_replaces_the_tasks_and_records_both(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    old_tasks = swarm.host.service_tasks(service_id)

    forced = await swarm.sw.service_actions.force_update(
        ADMIN, ENV_A, service_id, expected_version=version
    )

    row = await last_row(swarm, "service_force_updated")
    assert forced.tasks_running == 2
    assert stored(swarm, service_id)["TaskTemplate"]["ForceUpdate"] == 1
    assert row["detail"]["force_update_before"] == 0 and row["detail"]["force_update_after"] == 1
    assert row["detail"]["tasks_running"] == 2
    assert swarm.host.registry_auth_from[-1] == "spec"
    assert {swarm.host.tasks[t]["DesiredState"] for t in old_tasks} == {"shutdown"}


async def test_a_force_update_of_a_stale_review_is_refused(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)

    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.force_update(
            ADMIN, ENV_A, service_id, expected_version=version + 7
        )

    assert (await last_row(swarm, "service_force_updated"))["detail"]["reason"] == (
        "changed_since_review"
    )


async def test_an_edit_is_reviewed_by_category_then_applied_and_recorded(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    values = {**offer.values, "replicas": "3", "command": "sleep 200"}

    review = await swarm.sw.services.review(
        ADMIN, ENV_A, service_id, values, form_version=offer.version
    )
    updated = await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, values, expected_version=offer.version
    )

    row = await last_row(swarm, "service_updated")
    assert set(review.categories) == {"command", "mode"} and review.tasks_replaced
    assert set(updated.categories) == {"command", "mode"} and not updated.unchanged
    assert row["detail"]["image_changed"] is False and "command" in row["detail"]["changed"]
    assert swarm.host.registry_auth_from[-1] == "spec"
    assert stored(swarm, service_id)["TaskTemplate"]["ContainerSpec"]["Args"] == ["sleep", "200"]


async def test_an_edit_that_changes_nothing_sends_nothing_and_says_so(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    before = len(swarm.host.mutating())

    updated = await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, dict(offer.values), expected_version=offer.version
    )

    assert updated.unchanged and updated.categories == ()
    assert len(swarm.host.mutating()) == before


async def test_fields_the_form_does_not_model_survive_an_update(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm)
    container = stored(swarm, service_id)["TaskTemplate"]["ContainerSpec"]
    container["Healthcheck"] = {"Test": ["NONE"]}
    container["User"] = "1000"
    swarm.host.services[service_id]["Version"]["Index"] += 1
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)

    await swarm.sw.service_actions.update(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "sleep 300"},
        expected_version=offer.version,
    )

    kept = stored(swarm, service_id)["TaskTemplate"]["ContainerSpec"]
    assert kept["Healthcheck"] == {"Test": ["NONE"]} and kept["User"] == "1000"


async def test_a_review_built_from_an_older_form_version_is_refused_and_a_newer_one_passes(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    swarm.host.services[service_id]["Version"]["Index"] += 1
    swarm.fresh()

    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.services.review(
            ADMIN, ENV_A, service_id, offer.values, form_version=offer.version
        )
    review = await swarm.sw.services.review(
        ADMIN, ENV_A, service_id, offer.values, form_version=offer.version + 1
    )

    assert review.version == offer.version + 1


async def test_an_update_of_a_version_that_moved_is_refused_before_anything_is_sent(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    swarm.host.services[service_id]["Version"]["Index"] += 1
    before = len(swarm.host.mutating())

    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.update(
            ADMIN, ENV_A, service_id, dict(offer.values), expected_version=offer.version
        )

    assert len(swarm.host.mutating()) == before
    assert (await last_row(swarm, "service_updated"))["detail"]["reason"] == "changed_since_review"


async def test_a_500_on_update_is_decided_by_re_reading_not_by_the_engines_text(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    values = {**offer.values, "command": "sleep 400"}
    swarm.host.stale_next = True

    with pytest.raises(ServiceRefused):
        await swarm.sw.service_actions.update(
            ADMIN, ENV_A, service_id, values, expected_version=offer.version
        )
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    swarm.host.stale_next = True
    swarm.host.stale_moves_version = True
    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.update(
            ADMIN,
            ENV_A,
            service_id,
            {**offer.values, "command": "sleep 500"},
            expected_version=offer.version,
        )

    reasons = [r["detail"]["reason"] for r in await rows_of(swarm.db, "service_updated")]
    assert reasons == ["engine_rejected", "changed_since_review"]


async def test_switching_between_replicated_and_global_mode_is_a_field_error_on_mode(
    swarm: SwarmRig,
) -> None:
    global_id = await deploy(swarm, **GLOBAL_FORM)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, global_id)

    with pytest.raises(ServiceInvalid) as raised:
        await swarm.sw.services.review(
            ADMIN,
            ENV_A,
            global_id,
            {**offer.values, "mode": "replicated", "replicas": "2"},
            form_version=offer.version,
        )

    assert "mode" in raised.value.fields


async def test_a_legacy_top_level_networks_entry_moves_into_the_task_template_on_update(
    swarm: SwarmRig,
) -> None:
    network_id = swarm.host.add_network(
        "ov", 0, "overlay", network_id="z" + "5".zfill(24), Scope="swarm"
    )
    service_id = await deploy(swarm, **GLOBAL_FORM)
    swarm.host.services[service_id]["Spec"]["Networks"] = [{"Target": network_id}]
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)

    await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, {**offer.values, "command": "x y"}, expected_version=offer.version
    )

    spec = stored(swarm, service_id)
    assert offer.values["network-0-name"] == network_id
    assert "Networks" not in spec and spec["TaskTemplate"]["Networks"] == [{"Target": network_id}]


async def test_rolling_back_restores_the_previous_spec_and_reuses_the_stored_credentials(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    await swarm.sw.service_actions.update(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "sleep 9"},
        expected_version=offer.version,
    )
    swarm.fresh()
    impact = await swarm.sw.services.rollback_impact(ADMIN, ENV_A, service_id)

    await swarm.sw.service_actions.rollback(
        ADMIN, ENV_A, service_id, expected_version=impact.version
    )

    row = await last_row(swarm, "service_rolled_back")
    assert "command" in impact.categories
    assert stored(swarm, service_id)["TaskTemplate"]["ContainerSpec"]["Args"] == ["sleep", "100"]
    assert swarm.host.registry_auth_from[-1] == "previous-spec"
    assert row["result"] == "success" and "command" in row["detail"]["changed"]


async def test_rolling_back_a_service_that_was_never_updated_is_refused(swarm: SwarmRig) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)

    with pytest.raises(NoPreviousSpec):
        await swarm.sw.service_actions.rollback(ADMIN, ENV_A, service_id, expected_version=version)

    assert (await last_row(swarm, "service_rolled_back"))["detail"]["reason"] == "no_previous_spec"


async def test_removing_a_bound_service_deletes_it_and_releases_its_control(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    impact = await swarm.sw.services.remove_impact(ADMIN, ENV_A, service_id)

    removed = await swarm.sw.service_actions.remove(
        ADMIN, ENV_A, service_id, expected_name="web", expected_version=impact.version
    )

    row = await last_row(swarm, "service_removed")
    assert removed.control_released and service_id not in swarm.host.services
    assert (row["detail"]["name"], row["detail"]["control_released"], row["detail"]["tasks"]) == (
        "web",
        True,
        2,
    )
    assert await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "service", service_id)) is None


@pytest.mark.parametrize(
    ("name", "bump"),
    [pytest.param("other", 0, id="renamed"), pytest.param("web", 1, id="version moved")],
)
async def test_a_removal_that_does_not_match_what_was_reviewed_removes_nothing(
    swarm: SwarmRig, name: str, bump: int
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)

    with pytest.raises(ServiceChangedSinceReview):
        await swarm.sw.service_actions.remove(
            ADMIN, ENV_A, service_id, expected_name=name, expected_version=version + bump
        )

    assert service_id in swarm.host.services


async def test_a_service_already_gone_from_the_host_releases_its_control_and_says_so(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    swarm.host.remove_status = 404

    with pytest.raises(ServiceGone):
        await swarm.sw.service_actions.remove(
            ADMIN, ENV_A, service_id, expected_name="web", expected_version=version
        )

    row = await last_row(swarm, "service_removed")
    assert (row["result"], row["detail"]["reason"], row["detail"]["control_released"]) == (
        "failure",
        "service_gone",
        True,
    )
    assert await swarm.access.controls.get(ADMIN, ResourceKey(ENV_A, "service", service_id)) is None


async def test_a_removal_whose_reply_never_comes_is_unavailable_and_records_the_outcome_unknown(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    swarm.host.route("DELETE", f"/services/{service_id}", Stall())

    with pytest.raises(EnvironmentUnavailable):
        await swarm.sw.service_actions.remove(
            ADMIN, ENV_A, service_id, expected_name="web", expected_version=version
        )

    row = await last_row(swarm, "service_removed")
    assert (row["result"], row["detail"]["reason"]) == ("failure", "outcome_unknown")


@pytest.mark.parametrize("operation", ["scale", "force", "rollback", "remove", "update"])
async def test_an_operation_on_a_service_another_action_holds_is_busy_and_sends_nothing(
    swarm: SwarmRig, operation: str
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    holder = LockHolder(UserActor(PEER.user_id), "other-admin", "update")
    key = ObjectKey(ENV_A, "service", service_id)
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "scale": lambda: swarm.sw.service_actions.scale(
            ADMIN, ENV_A, service_id, replicas=3, expected_version=version, expected_replicas=2
        ),
        "force": lambda: swarm.sw.service_actions.force_update(
            ADMIN, ENV_A, service_id, expected_version=version
        ),
        "rollback": lambda: swarm.sw.service_actions.rollback(
            ADMIN, ENV_A, service_id, expected_version=version
        ),
        "remove": lambda: swarm.sw.service_actions.remove(
            ADMIN, ENV_A, service_id, expected_name="web", expected_version=version
        ),
        "update": lambda: swarm.sw.service_actions.update(
            ADMIN, ENV_A, service_id, dict(FORM), expected_version=version
        ),
    }
    before = len(swarm.host.mutating())

    async with swarm.engine.locks.hold(key, holder):
        with pytest.raises(ObjectBusy) as busy:
            await calls[operation]()

    view = swarm.sw.service_actions.describe_busy(busy.value, ADMIN)
    assert (view.action, view.holder, view.held_by_you) == ("update", "other-admin", False)
    assert len(swarm.host.mutating()) == before


async def test_a_create_cancelled_after_dispatch_still_writes_one_success_row_and_claims(
    swarm: SwarmRig,
) -> None:
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(swarm.sw.service_actions.create(ADMIN, ENV_A, FORM))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [row] = await rows_of(swarm.db, "service_created")
    [service_id] = list(swarm.host.services)
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert row["detail"]["control_created"] is True and row["target_id"] == service_id


async def test_a_create_still_unfinished_after_the_wait_is_outcome_unknown_then_settles_late(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services_module, "SETTLE_SECONDS", INTERRUPTED_SETTLE_SECONDS)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(swarm.sw.service_actions.create(ADMIN, ENV_A, FORM))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    first = await rows_of(swarm.db, "service_created")
    swarm.host.gate.set()
    await drain_background()

    rows = await rows_of(swarm.db, "service_created")
    assert [(r["result"], r["detail"].get("reason")) for r in first] == [
        ("failure", "outcome_unknown")
    ]
    assert [r["result"] for r in rows] == ["failure", "success"]
    assert rows[1]["detail"]["settled_late"] is True and rows[1]["detail"]["control_created"]


async def test_a_cancelled_remove_that_completed_still_releases_the_control(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(
        swarm.sw.service_actions.remove(
            ADMIN, ENV_A, service_id, expected_name="web", expected_version=version
        )
    )
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    row = await last_row(swarm, "service_removed")
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert row["detail"]["control_released"] is True and service_id not in swarm.host.services


@pytest.mark.parametrize("service_id", ["web", "A" * 25, "short", "../../x"])
async def test_a_name_shaped_or_malformed_service_id_is_one_not_found_for_reads_and_writes(
    swarm: SwarmRig, service_id: str
) -> None:
    await deploy(swarm)
    before = len(swarm.host.calls("POST"))

    with pytest.raises(ResourceNotFound) as read:
        await swarm.sw.services.detail(ADMIN, ENV_A, service_id)
    with pytest.raises(ResourceNotFound) as write:
        await swarm.sw.service_actions.force_update(ADMIN, ENV_A, service_id, expected_version=1)

    assert str(read.value) == str(write.value)
    assert len(swarm.host.calls("POST")) == before


async def test_a_well_formed_id_that_no_service_has_is_gone_for_a_write_and_not_found_for_a_read(
    swarm: SwarmRig,
) -> None:
    with pytest.raises(ResourceNotFound):
        await swarm.sw.services.detail(ADMIN, ENV_A, MISSING_ID)
    with pytest.raises(ServiceGone):
        await swarm.sw.service_actions.force_update(ADMIN, ENV_A, MISSING_ID, expected_version=1)

    row = await last_row(swarm, "service_force_updated")
    assert row["detail"]["reason"] == "service_gone"


async def test_a_service_named_like_another_services_id_never_resolves_to_it(
    swarm: SwarmRig,
) -> None:
    first = await deploy(swarm)
    swarm.host.add_service(plain_spec(first))
    swarm.fresh()

    detail = await swarm.sw.services.detail(ADMIN, ENV_A, first)

    assert detail.id == first and detail.name == "web"


async def test_a_standard_user_neither_sees_nor_touches_a_service_without_a_grant(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)

    with pytest.raises(ResourceNotFound) as read:
        await swarm.sw.services.detail(STANDARD, ENV_A, service_id)
    with pytest.raises(ResourceNotFound) as missing:
        await swarm.sw.services.detail(STANDARD, ENV_A, MISSING_ID)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.service_actions.remove(
            STANDARD, ENV_A, service_id, expected_name="web", expected_version=version
        )

    assert str(read.value) == str(missing.value)
    assert (await swarm.sw.services.page(STANDARD, ENV_A)).page.items == []


async def test_read_access_lets_a_standard_user_see_a_service_but_not_change_it(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("shared"))
    await control(
        swarm.access, ResourceKey(ENV_A, "service", service_id), users=(STANDARD.user_id,)
    )
    swarm.fresh()

    detail = await swarm.sw.services.detail(STANDARD, ENV_A, service_id)
    with pytest.raises(PermissionDenied):
        await swarm.sw.service_actions.force_update(
            STANDARD, ENV_A, service_id, expected_version=detail.version
        )

    assert detail.name == "shared" and not detail.can_write and not detail.can_scale
    assert not detail.can_manage_access


async def test_on_a_local_socket_a_standard_user_may_read_but_every_write_is_forbidden(
    swarm: SwarmRig,
) -> None:
    service_id = swarm.host.add_service(plain_spec("local"))
    await control(
        swarm.access,
        ResourceKey(ENV_B, "service", service_id),
        level="write",
        public=True,
    )
    swarm.fresh()

    detail = await swarm.sw.services.detail(STANDARD, ENV_B, service_id)
    with pytest.raises(PermissionDenied):
        await swarm.sw.service_actions.create(STANDARD, ENV_B, FORM)
    with pytest.raises(PermissionDenied):
        await swarm.sw.service_actions.scale(
            STANDARD,
            ENV_B,
            service_id,
            replicas=2,
            expected_version=detail.version,
            expected_replicas=1,
        )

    assert detail.name == "local" and not detail.can_write
    assert swarm.host.create_bodies == []


async def test_the_service_list_filters_by_name_mode_and_stack(swarm: SwarmRig) -> None:
    await deploy(swarm)
    await deploy(swarm, **GLOBAL_FORM)
    swarm.host.add_service(plain_spec("shop_api", Labels={NAMESPACE_LABEL: "Shop"}))

    everything = await swarm.sw.services.page(ADMIN, ENV_A)
    by_name = await swarm.sw.services.page(ADMIN, ENV_A, ServiceFilters(q="ag"))
    by_mode = await swarm.sw.services.page(ADMIN, ENV_A, ServiceFilters(mode="global"))
    by_stack = await swarm.sw.services.page(ADMIN, ENV_A, ServiceFilters(stack="shop"))

    assert [r.name for r in everything.page.items] == ["agent", "shop_api", "web"]
    assert [r.name for r in by_name.page.items] == ["agent"]
    assert [r.name for r in by_mode.page.items] == ["agent"]
    assert [r.name for r in by_stack.page.items] == ["shop_api"]
    assert everything.total == 3


async def test_the_service_detail_reports_replicas_version_and_the_scale_ceiling(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)

    detail = await swarm.sw.services.detail(ADMIN, ENV_A, service_id)

    assert (detail.replicas, detail.mode, detail.can_scale, detail.digest_pinned) == (
        2,
        "replicated",
        True,
        True,
    )
    assert detail.replicas_maximum == 1000
    assert detail.version == swarm.host.services[service_id]["Version"]["Index"]


async def start_one_task_late(rig: SwarmRig, service_id: str) -> None:
    live = [
        t
        for t in rig.host.service_tasks(service_id)
        if rig.host.tasks[t]["DesiredState"] == "running"
    ]
    rig.host.tasks[live[0]]["Status"]["State"] = "starting"
    rig.fresh()


async def test_task_counts_on_the_detail_review_and_rollback_come_from_the_status_list(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)
    await swarm.sw.service_actions.update(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "sleep 9"},
        expected_version=offer.version,
    )
    await start_one_task_late(swarm, service_id)

    detail = await swarm.sw.services.detail(ADMIN, ENV_A, service_id)
    review = await swarm.sw.services.review(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "sleep 10"},
        form_version=detail.version,
    )
    impact = await swarm.sw.services.rollback_impact(ADMIN, ENV_A, service_id)

    assert (detail.running, detail.desired) == (RUNNING_AFTER_ONE_STARTS_LATE, DESIRED_REPLICAS)
    assert review.running_tasks == RUNNING_AFTER_ONE_STARTS_LATE
    assert impact.running_tasks == RUNNING_AFTER_ONE_STARTS_LATE


async def test_task_counts_fall_back_to_counting_tasks_when_the_status_list_omits_the_service(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = await deploy(swarm)
    await start_one_task_late(swarm, service_id)

    async def nothing_listed(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(engine_resources.ServicesApi, "list", nothing_listed)

    detail = await swarm.sw.services.detail(ADMIN, ENV_A, service_id)

    assert (detail.running, detail.desired) == (RUNNING_AFTER_ONE_STARTS_LATE, DESIRED_REPLICAS)


async def test_the_tasks_table_groups_a_replicated_service_by_slot_and_a_global_one_by_node(
    swarm: SwarmRig,
) -> None:
    replicated = await deploy(swarm)
    agent = await deploy(swarm, **GLOBAL_FORM)

    slots = await swarm.sw.services.tasks(ADMIN, ENV_A, replicated)
    nodes = await swarm.sw.services.tasks(ADMIN, ENV_A, agent)

    assert slots.shown == 2 and [g.label for g in slots.groups] == ["Slot 1", "Slot 2"]
    assert nodes.shown == 2 and {t.node_hostname for g in nodes.groups for t in g.tasks} == {
        "mgr",
        "wrk",
    }


async def test_the_tasks_poll_does_not_read_the_service_detail(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = await deploy(swarm)

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("detail was read by the tasks poll")

    monkeypatch.setattr(type(swarm.sw.services), "detail", boom)

    table = await swarm.sw.services.tasks(ADMIN, ENV_A, service_id)

    assert table.shown == 2


async def test_a_task_waiting_to_reach_its_desired_state_makes_the_table_poll(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    settled = await swarm.sw.services.tasks(ADMIN, ENV_A, service_id)
    swarm.host.tasks[swarm.host.service_tasks(service_id)[0]]["DesiredState"] = "ready"
    swarm.fresh()

    moving = await swarm.sw.services.tasks(ADMIN, ENV_A, service_id)

    assert (settled.polling, moving.polling) == (False, True)


async def test_the_node_hostname_of_a_replicated_service_follows_its_placement(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm, constraints=f"node.id=={MANAGER}")

    table = await swarm.sw.services.tasks(ADMIN, ENV_A, service_id)

    assert {t.node_id for g in table.groups for t in g.tasks} == {MANAGER}
    assert WORKER not in {t.node_id for g in table.groups for t in g.tasks}


@pytest.mark.parametrize(
    ("setup", "expected", "reason"),
    [
        pytest.param(
            {"outage": {"tasks"}}, SwarmUnavailable, "not_swarm_manager", id="not a manager"
        ),
        pytest.param({"cap": ("TASK_LIST_BYTES", 16)}, ObjectTooLarge, "too_large", id="too large"),
    ],
)
async def test_a_swarm_read_that_fails_inside_a_write_leaves_a_listed_reason(
    swarm: SwarmRig,
    monkeypatch: pytest.MonkeyPatch,
    setup: dict[str, Any],
    expected: type[Exception],
    reason: str,
) -> None:
    service_id = await deploy(swarm)
    version = await version_of(swarm, service_id)
    swarm.host.outage = setup.get("outage", set())
    if "cap" in setup:
        monkeypatch.setattr(engine_resources, *setup["cap"])

    with pytest.raises(expected):
        await swarm.sw.service_actions.force_update(
            ADMIN, ENV_A, service_id, expected_version=version
        )

    row = await last_row(swarm, "service_force_updated")
    assert (row["result"], row["detail"]["reason"]) == ("failure", reason)


async def test_stored_values_outside_the_forms_ranges_stay_editable_and_survive_the_update(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm)
    task = stored(swarm, service_id)["TaskTemplate"]
    task["Resources"] = {"Limits": {"NanoCPUs": 500_000, "MemoryBytes": 1024}}
    task["RestartPolicy"] = {"Condition": "any", "Delay": 1_500_400}
    stored(swarm, service_id)["UpdateConfig"] = {"Delay": 500_000, "Parallelism": 1}
    swarm.fresh()
    offer = await swarm.sw.services.edit_offer(ADMIN, ENV_A, service_id)

    review = await swarm.sw.services.review(
        ADMIN, ENV_A, service_id, offer.values, form_version=offer.version
    )
    updated = await swarm.sw.service_actions.update(
        ADMIN,
        ENV_A,
        service_id,
        {**offer.values, "command": "sleep 3"},
        expected_version=offer.version,
    )
    swarm.fresh()
    with pytest.raises(ValidationFailed) as invalid:
        await swarm.sw.services.review(
            ADMIN,
            ENV_A,
            service_id,
            {**offer.values, "cpu_limit": "0.0001"},
            form_version=offer.version + 1,
        )

    kept = stored(swarm, service_id)
    assert (
        offer.values["restart_delay"] == "1ms500us400ns" and offer.values["update_delay"] == "500us"
    )
    assert review.categories == () and updated.categories == ("command",)
    assert kept["TaskTemplate"]["Resources"]["Limits"] == {"NanoCPUs": 500_000, "MemoryBytes": 1024}
    assert kept["TaskTemplate"]["RestartPolicy"]["Delay"] == 1_500_400
    assert kept["UpdateConfig"]["Delay"] == 500_000
    assert "cpu_limit" in invalid.value.fields


async def test_only_a_task_on_the_connected_manager_whose_container_is_visible_links_to_it(
    swarm: SwarmRig,
) -> None:
    service_id = await deploy(swarm, replicas="2")
    on_manager, on_worker = (swarm.host.tasks[t] for t in swarm.host.service_tasks(service_id))
    on_worker["NodeID"] = WORKER
    swarm.host.add(
        on_manager["Status"]["ContainerStatus"]["ContainerID"], "task-box", state="running"
    )
    swarm.host.add(
        on_worker["Status"]["ContainerStatus"]["ContainerID"], "worker-box", state="running"
    )

    table = await swarm.sw.services.tasks(ADMIN, ENV_A, service_id)

    links = {t.node_id: t.container_link for g in table.groups for t in g.tasks}
    assert links == {MANAGER: True, WORKER: False}


LEGACY = [{"Target": "a" * 25}]
CURRENT = [{"Target": "b" * 25}]


@pytest.mark.parametrize(
    ("spec", "networks"),
    [
        pytest.param({"Networks": LEGACY, "TaskTemplate": {}}, LEGACY, id="moved"),
        pytest.param({"Networks": LEGACY}, LEGACY, id="no task template yet"),
        pytest.param(
            {"Networks": LEGACY, "TaskTemplate": {"Networks": CURRENT}},
            CURRENT,
            id="task wins",
        ),
        pytest.param({"Networks": [], "TaskTemplate": {}}, None, id="empty legacy dropped"),
        pytest.param({"Networks": "x", "TaskTemplate": {}}, None, id="garbage dropped"),
        pytest.param({"TaskTemplate": {"Networks": CURRENT}}, CURRENT, id="nothing to move"),
    ],
)
def test_the_legacy_top_level_networks_entry_moves_into_the_task_only_when_that_is_empty(
    spec: dict[str, Any], networks: list[dict[str, str]] | None
) -> None:
    migrated = migrate_legacy_networks(spec)

    assert migrated.get("TaskTemplate", {}).get("Networks") == networks
    assert "Networks" not in migrated


def test_migrating_leaves_its_input_untouched() -> None:
    spec: dict[str, Any] = {"Networks": LEGACY, "TaskTemplate": {}}

    migrate_legacy_networks(spec)

    assert spec == {"Networks": LEGACY, "TaskTemplate": {}}


@pytest.mark.parametrize(
    ("image", "shown"),
    [
        ("busybox:latest", "busybox:latest"),
        (f"busybox:latest@sha256:{'a' * 64}", f"busybox:latest@sha256:{'a' * 12}"),
        ("busybox@md5:abc", "busybox@md5:abc"),
    ],
)
def test_a_digest_is_shortened_for_display_only_when_it_is_a_sha256(image: str, shown: str) -> None:
    assert display_image(image) == shown
