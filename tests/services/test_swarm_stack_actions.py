"""Tests for app/services/swarm_stack_actions.py over a fake manager and a Compose that resolves."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import app.services.swarm_stack_actions as actions_module
from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services.access import ResourceKey
from app.services.activity import UserActor
from app.services.assembly import start_stack_job
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey
from app.services.stack_deploy import ChangedSinceReview, StackNameInUse, StackRejected
from app.services.swarm_stack_outcomes import SwarmStackOutcome
from tests import stack_support
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    PEER,
    STANDARD,
    control,
    stack_key,
    volume_key,
)
from tests.docker_engine.swarm_engine import MANAGER
from tests.environment_support import RecordingPoker
from tests.image_support import REFERENCE, REFERENCE_KEY, binding_rows, decode_auth, register
from tests.object_support import drain_background
from tests.stack_support import Rig, wait_job
from tests.support import present
from tests.swarm_stack_support import (
    NAME,
    NAMESPACE_LABEL,
    SECRET_VALUE,
    StackDaemon,
    compose_commands,
    create_stack,
    deploy_stack,
    kept_secret_variables,
    namespaced_service,
    redeploy_stack,
    remove_stack,
    reviewed_plan,
    shop_file,
    shop_variables,
    simple_file,
    swarm_services,
)

LABELS = {NAMESPACE_LABEL: NAME}
ANONYMOUS_AUTH = "e30="
SETTLE_BEYOND_THE_WINDOW = -19.9
SHORT_DEADLINE_SECONDS = 0.2
FAST_RETRY_SECONDS = 0.01
ENGINE_DETAIL = "engine detail"
NETWORK_NAME = "Shop_default"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions_module, "NETWORK_RETRY_SECONDS", FAST_RETRY_SECONDS)


def compose_directory(rig: Rig) -> Path:
    return rig.data_dir.parent / "compose"


async def rows(rig: Rig, action: str | None = None) -> list[dict[str, Any]]:
    return await rig.rows(action)


async def count(rig: Rig, table: str) -> int:
    found = await rig.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
    assert found is not None
    return int(found["n"])


def stack_lock_key() -> ObjectKey:
    return ObjectKey(ENV_A, "stack", NAME.lower())


async def can(rig: Rig, who: Any, key: ResourceKey, level: Literal["read", "write"]) -> bool:
    try:
        await swarm_services(rig).access.authoriser.require_access(who, key, level)
    except (ResourceNotFound, PermissionDenied):
        return False
    return True


class TestCreateAndDeploy:
    async def test_a_created_stack_deploys_as_a_job_labelled_in_every_object_it_creates(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        created = await create_stack(rig)
        assert created.control_created and created.name == NAME

        view = await deploy_stack(rig)

        assert view.state == "succeeded", view.outcome
        result = view.outcome.result
        assert isinstance(result, SwarmStackOutcome)
        assert sorted(result.created) == sorted(
            [
                "network:Shop_front",
                "network:Shop_default",
                *(f"config:{n}" for n in stack_daemon.config_names()),
                *(f"secret:{n}" for n in stack_daemon.secret_names()),
                "service:Shop_web",
                "service:Shop_api",
            ]
        )
        web = stack_daemon.service("Shop_web")
        container = web["TaskTemplate"]["ContainerSpec"]
        assert web["Labels"][NAMESPACE_LABEL] == NAME
        assert container["Labels"][NAMESPACE_LABEL] == NAME
        assert container["Configs"][0]["ConfigName"] in stack_daemon.config_names()
        assert container["Secrets"][0]["SecretName"].startswith("Shop_sec_")
        for table in (stack_daemon.configs, stack_daemon.secrets):
            assert all(o["Spec"]["Labels"][NAMESPACE_LABEL] == NAME for o in table.values())
        assert all(
            n["Labels"][NAMESPACE_LABEL] == NAME
            for n in stack_daemon.networks.values()
            if n["Name"].startswith("Shop_")
        )
        resources = [r.resource for r in view.progress.resources]
        assert "Network Shop_front" in resources and "Service Shop_web" in resources

    async def test_only_the_config_command_is_ever_run_and_nothing_is_a_stack_deploy_or_up(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await redeploy_stack(swarm_stack_rig)
        await remove_stack(swarm_stack_rig)

        commands = compose_commands(compose_directory(swarm_stack_rig))

        assert set(commands) <= {"version", "config"}
        assert "config" in commands

    async def test_every_step_records_a_swarm_row_and_the_stack_row_settles(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)

        [created] = await rows(swarm_stack_rig, "stack_created")
        [deployed] = await rows(swarm_stack_rig, "stack_deployed")
        stored = present(
            await swarm_stack_rig.db.fetch_one("SELECT status, last_operation, kind FROM stack")
        )

        assert created["detail"]["kind"] == deployed["detail"]["kind"] == "swarm"
        assert deployed["result"] == "success"
        assert tuple(stored) == ("active", "deploy", "swarm")

    async def test_a_job_started_for_a_swarm_stack_is_tagged_with_its_kind(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        job = start_stack_job(
            swarm_stack_rig.app,
            ADMIN,
            ENV_A,
            "create",
            NAME,
            lambda svc, progress: svc.swarm_actions.deploy(ADMIN, ENV_A, NAME, sink=progress),
            stack_kind="swarm",
        )

        view = await wait_job(swarm_stack_rig, ADMIN, job)

        assert view.stack_kind == "swarm"

    async def test_a_first_deploy_whose_plan_has_a_remove_line_is_refused_before_any_write(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        stack_daemon.add_config("Shop_old_abcdefabcdef", labels=LABELS)

        with pytest.raises(ChangedSinceReview):
            await swarm_stack_rig.fresh().swarm_actions.deploy(ADMIN, ENV_A, NAME)

        assert stack_daemon.mutating() == []

    async def test_a_secret_reaches_the_engine_only_as_the_secret_data(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)

        sent = [
            base64.b64decode(b["Data"])
            for kind, b in stack_daemon.create_bodies
            if kind == "secrets"
        ]

        assert sent == [SECRET_VALUE.encode()]


class TestRedeploy:
    async def test_an_unchanged_redeploy_writes_nothing_to_the_cluster(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        before = list(stack_daemon.mutating())

        view = await redeploy_stack(swarm_stack_rig)

        assert view.state == "succeeded", view.outcome
        assert stack_daemon.mutating() == before
        result = view.outcome.result
        assert result.created == result.updated == result.removed == ()
        assert sorted(result.unchanged) == ["service:Shop_api", "service:Shop_web"]
        [redeployed] = await rows(swarm_stack_rig, "stack_redeployed")
        assert redeployed["result"] == "success" and redeployed["detail"]["unchanged_count"] == 2

    async def test_a_kept_network_the_engine_no_longer_has_is_created_before_the_services(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        [network_id] = [i for i, n in stack_daemon.networks.items() if n["Name"] == NETWORK_NAME]
        stack_daemon.linger_network(network_id)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3)
        )
        before = len(stack_daemon.mutating())

        view = await redeploy_stack(swarm_stack_rig)

        assert view.state == "succeeded", view.outcome
        assert view.outcome.result.created == (f"network:{NETWORK_NAME}",)
        [recreated] = [n for n in stack_daemon.networks.values() if n["Name"] == NETWORK_NAME]
        assert recreated["Labels"][NAMESPACE_LABEL] == NAME
        order = stack_daemon.mutating()[before:]
        assert order.index("POST /networks/create") < min(
            i for i, entry in enumerate(order) if entry.endswith("/update")
        )
        [row] = await rows(swarm_stack_rig, "stack_redeployed")
        assert row["detail"]["created"] == f"network/{NETWORK_NAME}"

    async def test_a_changed_config_and_secret_rotate_and_the_old_objects_go_after_the_updates(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        old_configs, old_secrets = stack_daemon.config_names(), stack_daemon.secret_names()
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=shop_variables("config v2", SECRET_VALUE + "-new")
        )

        view = await redeploy_stack(swarm_stack_rig)

        assert view.state == "succeeded", view.outcome
        result = view.outcome.result
        assert "service:Shop_web" in result.updated and "service:Shop_api" not in result.updated
        assert set(stack_daemon.config_names()).isdisjoint(old_configs)
        assert set(stack_daemon.secret_names()).isdisjoint(old_secrets)
        container = stack_daemon.service("Shop_web")["TaskTemplate"]["ContainerSpec"]
        assert {c["ConfigID"] for c in container["Configs"]} <= set(stack_daemon.configs)
        assert {s["SecretID"] for s in container["Secrets"]} <= set(stack_daemon.secrets)
        order = stack_daemon.mutating()
        last_update = max(i for i, entry in enumerate(order) if entry.endswith("/update"))
        first_delete = min(i for i, entry in enumerate(order) if entry.startswith("DELETE"))
        assert first_delete > last_update and len(stack_daemon.mutations("DELETE")) == 2
        again = await redeploy_stack(swarm_stack_rig)
        assert again.outcome.result.created == ()

    async def test_an_old_config_a_service_outside_the_stack_uses_is_kept_and_reported(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        [(old_id, old)] = [(i, c["Spec"]["Name"]) for i, c in stack_daemon.configs.items()]
        stack_daemon.named_service("outside", configs=[(old_id, old)])
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=kept_secret_variables("config v2")
        )

        view = await redeploy_stack(swarm_stack_rig)

        assert view.state == "succeeded", view.outcome
        assert view.outcome.result.kept == (f"config:{old}",)
        assert old in stack_daemon.config_names() and len(stack_daemon.configs) == 2
        [row] = await rows(swarm_stack_rig, "stack_redeployed")
        assert row["detail"]["kept"] == f"config/{old}"

    async def test_a_service_outside_the_file_is_removed_only_when_the_tick_is_given(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(api=False)
        )

        kept = await redeploy_stack(swarm_stack_rig)
        assert kept.state == "succeeded" and "Shop_api" in stack_daemon.service_names()
        gone = await redeploy_stack(swarm_stack_rig, orphans=True)

        assert gone.state == "succeeded", gone.outcome
        assert gone.outcome.result.removed == ("service:Shop_api", "network:Shop_default")
        assert stack_daemon.service_names() == ["Shop_web"]

    async def test_a_plan_that_changed_since_the_review_refuses_before_any_write(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        row, plan = await reviewed_plan(swarm_stack_rig)
        stack_daemon.add_secret("Shop_stray_aaaaaaaaaaaa", labels=LABELS)
        before = list(stack_daemon.mutating())

        with pytest.raises(ChangedSinceReview):
            await swarm_stack_rig.fresh().swarm_actions.redeploy(
                ADMIN,
                ENV_A,
                NAME,
                expected_file_digest=row.compose_file_digest,
                expected_inputs_digest=row.inputs_digest or "",
                expected_plan=plan.digest,
                remove_orphans=False,
            )

        assert stack_daemon.mutating() == before

    async def test_a_file_saved_after_the_review_refuses_the_redeploy(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        row, plan = await reviewed_plan(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=5)
        )
        before = list(stack_daemon.mutating())

        with pytest.raises(ChangedSinceReview):
            await swarm_stack_rig.fresh().swarm_actions.redeploy(
                ADMIN,
                ENV_A,
                NAME,
                expected_file_digest=row.compose_file_digest,
                expected_inputs_digest=row.inputs_digest or "",
                expected_plan=plan.digest,
                remove_orphans=False,
            )

        assert stack_daemon.mutating() == before

    async def test_variables_saved_after_the_review_refuse_the_redeploy(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        row, plan = await reviewed_plan(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=shop_variables("config v2", SECRET_VALUE)
        )
        before = list(stack_daemon.mutating())

        with pytest.raises(ChangedSinceReview):
            await swarm_stack_rig.fresh().swarm_actions.redeploy(
                ADMIN,
                ENV_A,
                NAME,
                expected_file_digest=row.compose_file_digest,
                expected_inputs_digest=row.inputs_digest or "",
                expected_plan=plan.digest,
            )

        assert stack_daemon.mutating() == before

    @pytest.mark.parametrize(
        ("moves", "reason"),
        [(True, "changed_since_review"), (False, "engine_rejected")],
        ids=["the-version-moved", "the-version-held"],
    )
    async def test_a_refused_update_is_decided_by_a_re_read(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon, moves: bool, reason: str
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3)
        )
        stack_daemon.stale_next = True
        stack_daemon.stale_moves_version = moves

        view = await redeploy_stack(swarm_stack_rig)

        assert view.state == "failed" and view.outcome.reason == reason, view.outcome
        [row] = await rows(swarm_stack_rig, "stack_redeployed")
        assert row["detail"]["reason"] == reason
        assert row["detail"]["failed_object"] == "service/Shop_web"

    async def test_the_force_update_counter_of_the_stored_service_is_carried_over(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        stack_daemon.service("Shop_web")["TaskTemplate"]["ForceUpdate"] = 4
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3)
        )

        await redeploy_stack(swarm_stack_rig)

        assert stack_daemon.service("Shop_web")["TaskTemplate"]["ForceUpdate"] == 4


class TestUpdate:
    async def test_saving_stores_the_file_and_variables_and_a_no_op_records_no_event(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        stored_digest = (
            present(await swarm_stack_rig.db.fetch_one("SELECT compose_file_digest FROM stack"))
        )["compose_file_digest"]

        unchanged = await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, expected_file_digest=stored_digest
        )
        variables_only = await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=kept_secret_variables(config="changed")
        )
        file_only = await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3)
        )

        saved = present(
            await swarm_stack_rig.db.fetch_one(
                "SELECT compose_file, compose_file_digest FROM stack"
            )
        )
        events = present(await rows(swarm_stack_rig, "stack_updated"))
        assert not unchanged.changed
        assert (variables_only.file_changed, variables_only.variables_changed) == (False, 1)
        assert (file_only.file_changed, file_only.variables_changed) == (True, 0)
        assert saved["compose_file"] == shop_file(web_replicas=3)
        assert saved["compose_file_digest"] == events[1]["detail"]["compose_digest"]
        assert [e["detail"]["file_changed"] for e in events] == [False, True]
        assert [e["detail"]["variables_changed"] for e in events] == [1, 0]
        assert {e["detail"]["kind"] for e in events} == {"swarm"}
        assert {e["detail"]["compose_version"] for e in events} == {
            present(swarm_stack_rig.compose.version).text
        }


class TestPartialFailure:
    async def test_a_failure_is_reported_per_object_and_a_redeploy_converges(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        stack_daemon.refuse_create.add("Shop_web")

        view = await deploy_stack(swarm_stack_rig)

        assert view.state == "failed" and view.outcome.reason == "engine_rejected"
        assert "Shop_web" in view.outcome.message and ENGINE_DETAIL not in view.outcome.message
        states = {r.resource: (r.status, r.text) for r in view.progress.resources}
        assert states["Service Shop_api"] == ("done", "Created")
        assert states["Service Shop_web"] == ("error", "Failed")
        result = view.outcome.result
        assert result.failed_object == "service:Shop_web" and "service:Shop_api" in result.created
        [failed] = await rows(swarm_stack_rig, "stack_deployed")
        assert failed["result"] == "failure"
        assert failed["detail"]["failed_object"] == "service/Shop_web"
        stored = present(
            await swarm_stack_rig.db.fetch_one("SELECT status, last_outcome FROM stack")
        )
        assert tuple(stored) == ("error", "failure")
        assert stack_daemon.service_names() == ["Shop_api"]
        stack_daemon.refuse_create.clear()

        again = await redeploy_stack(swarm_stack_rig)

        assert again.state == "succeeded", again.outcome
        assert again.outcome.result.created == ("service:Shop_web",)
        stored = present(await swarm_stack_rig.db.fetch_one("SELECT status FROM stack"))
        assert stored["status"] == "active"

    async def test_a_host_that_stopped_being_a_manager_is_named_and_poked(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        stack_daemon.create_status = 503

        view = await deploy_stack(swarm_stack_rig)

        assert view.state == "failed" and view.outcome.reason == "not_swarm_manager"
        assert ENV_A in cast(RecordingPoker, swarm_stack_rig.engine.refresher).poked


class TestRemove:
    async def test_remove_names_exact_targets_keeps_volumes_and_copies_the_control_to_them(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        stack_daemon.add_volume("Shop_data", labels=LABELS)
        row = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await rig.fresh().swarm_planner.remove_plan(ADMIN, ENV_A, row)
        assert plan.volumes_kept == ("Shop_data",)
        stack_daemon.network_busy = 1

        view = await remove_stack(rig)

        assert view.state == "succeeded", view.outcome
        assert stack_daemon.network_remove_calls >= 3
        assert not stack_daemon.services and not stack_daemon.configs and not stack_daemon.secrets
        assert "Shop_data" in stack_daemon.volumes
        assert stack_daemon.mutations("DELETE /volumes") == []
        assert await count(rig, "stack") == 0
        controls = await rig.db.fetch_limited(
            "SELECT resource_type, resource_id FROM resource_control", (), limit=20
        )
        assert [(c["resource_type"], c["resource_id"]) for c in controls] == [
            ("volume", "Shop_data")
        ]
        [removed] = await rows(rig, "stack_removed")
        assert removed["result"] == "success"
        assert removed["detail"]["controls_copied"] == 1 and removed["detail"]["volumes_kept"] == 1

    async def test_a_network_that_stays_busy_is_kept_reported_and_the_row_is_still_deleted(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        stack_daemon.network_busy = 99

        view = await remove_stack(swarm_stack_rig)

        assert view.state == "succeeded", view.outcome
        assert len(view.outcome.result.kept) == 2
        assert all(k.startswith("network:") for k in view.outcome.result.kept)
        assert stack_daemon.network_remove_calls == 4
        assert await count(swarm_stack_rig, "stack") == 0
        [removed] = await rows(swarm_stack_rig, "stack_removed")
        assert removed["detail"]["networks_kept"] == 2

    async def test_objects_a_remove_could_not_delete_keep_the_access_control_of_the_stack(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        file_text = shop_file()
        await create_stack(rig, file_text)
        await deploy_stack(rig)
        secret_id = next(iter(stack_daemon.secrets))
        network_id = next(i for i, n in stack_daemon.networks.items() if n["Name"] == "Shop_front")
        stack_daemon.busy_secrets.add(secret_id)
        stack_daemon.busy_networks.add(network_id)
        await swarm_services(rig).access.policy.set_uncontrolled_default(ADMIN, "write")
        secret = ResourceKey(ENV_A, "secret", secret_id)
        network = ResourceKey(ENV_A, "network", network_id)
        assert not await can(rig, STANDARD, secret, "read")

        view = await remove_stack(rig)

        assert view.state == "succeeded", view.outcome
        assert any(k.startswith("secret:") for k in view.outcome.result.kept)
        assert not await can(rig, STANDARD, secret, "read")
        assert not await can(rig, STANDARD, network, "read")
        copies = await rig.db.fetch_limited(
            "SELECT resource_type FROM resource_control ORDER BY resource_type", (), limit=20
        )
        assert [c["resource_type"] for c in copies] == ["network", "secret"]

    async def test_the_own_controls_of_removed_members_are_released(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        web_id = stack_daemon.service_id("Shop_web")
        await control(
            swarm_services(rig).access,
            ResourceKey(ENV_A, "service", web_id),
            administrators_only=True,
        )

        view = await remove_stack(rig)

        assert view.state == "succeeded", view.outcome
        assert view.outcome.result.controls_released == 1
        assert await count(rig, "resource_control") == 0

    async def test_a_member_set_that_changed_since_the_review_refuses_the_remove(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        row = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await rig.fresh().swarm_planner.remove_plan(ADMIN, ENV_A, row)
        stack_daemon.add_secret("Shop_extra", labels=LABELS)
        before = list(stack_daemon.mutating())

        with pytest.raises(ChangedSinceReview):
            await rig.fresh().swarm_actions.remove(
                ADMIN, ENV_A, NAME, stack_id=row.id, expected_members=plan.digest
            )

        assert stack_daemon.mutating() == before
        [refused] = await rows(rig, "stack_removed")
        assert refused["detail"]["reason"] == "changed_since_review"

    async def test_a_cancelled_remove_finishes_and_deletes_the_stack(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        row = await rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await rig.fresh().swarm_planner.remove_plan(ADMIN, ENV_A, row)
        stack_daemon.gate = asyncio.Event()
        task = asyncio.create_task(
            rig.fresh().swarm_actions.remove(
                ADMIN, ENV_A, NAME, stack_id=row.id, expected_members=plan.digest
            )
        )
        await asyncio.wait_for(stack_daemon.arrived.wait(), 10)
        task.cancel()
        stack_daemon.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain_background()

        assert not stack_daemon.services and not stack_daemon.configs
        [removed] = await rows(rig, "stack_removed")
        assert removed["result"] == "success" and removed["detail"]["interrupted"] is True
        assert await count(rig, "stack") == 0
        assert rig.engine.locks.holder_of(stack_lock_key()) is None


class TestForgetAndAdopt:
    async def test_forget_leaves_the_cluster_alone_and_keeps_the_stack_control(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        created = await create_stack(rig)
        await deploy_stack(rig)
        before = list(stack_daemon.mutating())

        await rig.fresh().swarm_actions.forget(ADMIN, ENV_A, NAME, stack_id=created.stack_id)

        assert stack_daemon.mutating() == before and len(stack_daemon.services) == 2
        assert await count(rig, "stack") == 0
        assert await count(rig, "resource_control") == 1
        [row] = await rows(rig, "stack_forgotten")
        assert row["detail"]["control_kept"] is True and row["detail"]["kind"] == "swarm"

    async def test_adopt_records_only_and_a_second_adoption_under_another_case_is_refused(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        stack_daemon.add_service(namespaced_service("Cli_web", "Cli"))
        stack_daemon.add_config("Cli_cfg", labels={NAMESPACE_LABEL: "Cli"})
        before = list(stack_daemon.mutating())

        adopted = await rig.fresh().swarm_adopter.adopt(
            ADMIN, ENV_A, "Cli", compose_text=shop_file(api=False), variables=shop_variables()
        )

        assert stack_daemon.mutating() == before
        assert adopted.containers == 2 and adopted.control_created is True
        [row] = await rows(rig, "stack_adopted")
        assert row["detail"]["members"] == 2 and row["detail"]["kind"] == "swarm"
        stored = present(
            await rig.db.fetch_one("SELECT origin, kind, status, last_operation FROM stack")
        )
        assert tuple(stored) == ("adopted", "swarm", "unknown", "adopt")
        with pytest.raises(StackNameInUse):
            await rig.fresh().swarm_adopter.adopt(
                ADMIN, ENV_A, "cli", compose_text=shop_file(), variables=shop_variables()
            )


class TestNamesAcrossKinds:
    async def test_a_name_that_folds_onto_a_stack_or_a_namespace_is_refused_as_is_a_bad_grammar(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        stack_daemon.add_secret("Other_x", labels={NAMESPACE_LABEL: "Other"})

        with pytest.raises(StackNameInUse):
            await create_stack(swarm_stack_rig, name="shop")
        with pytest.raises(StackNameInUse):
            await create_stack(swarm_stack_rig, name="other")
        with pytest.raises(StackRejected):
            await create_stack(swarm_stack_rig, name="bad.name")

    async def test_a_namespace_carried_only_by_a_volume_blocks_a_create_and_keeps_the_control(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        stack_daemon.add_volume("db_data", labels={NAMESPACE_LABEL: "db"})
        await control(swarm_services(rig).access, stack_key("db"), administrators_only=True)
        assert not await can(rig, STANDARD, volume_key("db_data"), "read")

        with pytest.raises(StackNameInUse):
            await create_stack(rig, simple_file(), [], name="DB", who=STANDARD)

        assert not await can(rig, STANDARD, volume_key("db_data"), "read")
        assert await count(rig, "stack") == 0
        controls = await rig.db.fetch_limited(
            "SELECT resource_id FROM resource_control WHERE resource_type = 'stack'", (), limit=5
        )
        assert [c["resource_id"] for c in controls] == ["db"]

    async def test_a_namespace_carried_only_by_a_container_label_blocks_a_create(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_volume("old_data", labels={"com.docker.compose.project": "Old"})

        with pytest.raises(StackNameInUse):
            await create_stack(swarm_stack_rig, simple_file(), [], name="old")

    async def test_the_namespace_check_follows_the_daemons_swarm_state_not_the_stored_type(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await rig.db.execute(
            "UPDATE environment SET type = 'standalone', swarm_state = 'active', "
            "swarm_control_available = 1 WHERE id = 1"
        )
        stack_daemon.named_service("Taken_web", labels={NAMESPACE_LABEL: "Taken"})
        client = await rig.inspector.hosts.client(ENV_A)

        with pytest.raises(StackNameInUse):
            await rig.fresh().queries.require_free(ENV_A, "taken", client)

    async def test_a_failed_namespace_read_means_the_name_is_not_free(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        from app.services.object_inspector import EnvironmentUnavailable

        stack_daemon.outage.add("services")
        client = await swarm_stack_rig.inspector.hosts.client(ENV_A)

        with pytest.raises(EnvironmentUnavailable):
            await swarm_stack_rig.fresh().queries.require_free(ENV_A, "anything", client)
        assert await count(swarm_stack_rig, "stack") == 0


class TestRefusalsAtCreate:
    async def test_a_secret_value_in_a_command_is_refused_and_the_variables_are_not_echoed(
        self, swarm_stack_rig: Rig
    ) -> None:
        text = simple_file(command=["run", SECRET_VALUE])

        with pytest.raises(StackRejected) as raised:
            await create_stack(
                swarm_stack_rig, text, stack_support.form_variables(("SECVAR", SECRET_VALUE, True))
            )

        assert raised.value.reason == "secret_in_config"
        assert SECRET_VALUE not in str(raised.value.fields)

    async def test_a_secret_value_in_the_environment_is_stored(self, swarm_stack_rig: Rig) -> None:
        text = simple_file(environment=["T=${SECVAR}"])

        created = await create_stack(
            swarm_stack_rig,
            text,
            stack_support.form_variables(("SECVAR", SECRET_VALUE, True)),
            name="Envs",
        )

        assert created.name == "Envs"

    async def test_a_bind_source_under_the_data_directory_or_relative_is_refused(
        self, swarm_stack_rig: Rig
    ) -> None:
        data = swarm_stack_rig.data_dir.as_posix()
        beneath = simple_file(
            volumes=[{"type": "bind", "source": f"{data}/secret.db", "target": "/x"}]
        )
        relative = simple_file(volumes=[{"type": "bind", "source": "../other", "target": "/x"}])

        with pytest.raises(StackRejected) as under:
            await create_stack(swarm_stack_rig, beneath, [], name="Bind")
        with pytest.raises(StackRejected):
            await create_stack(swarm_stack_rig, relative, [], name="Rel")

        assert any("services.web" in key for key in under.value.fields)

    async def test_a_standard_user_on_the_local_socket_cannot_create(
        self, swarm_stack_rig: Rig
    ) -> None:
        with pytest.raises((PermissionDenied, ResourceNotFound)):
            await swarm_stack_rig.fresh().swarm_actions.create(
                STANDARD, ENV_B, name="Local", compose_text=shop_file(), variables=shop_variables()
            )

    async def test_a_hand_built_restricted_file_is_refused_with_its_path_and_nothing_is_created(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        with pytest.raises(StackRejected) as raised:
            await create_stack(
                swarm_stack_rig, simple_file(cap_add=["SYS_ADMIN"]), [], name="Mine", who=STANDARD
            )

        assert "services.web.cap_add" in raised.value.fields
        assert await count(swarm_stack_rig, "stack") == 0 and stack_daemon.mutating() == []


class TestStandardUserOwnership:
    async def test_a_standard_user_creates_deploys_and_is_alone_with_the_stack(
        self, swarm_stack_rig: Rig
    ) -> None:
        rig = swarm_stack_rig
        created = await create_stack(rig, who=STANDARD)
        assert created.control_created

        view = await deploy_stack(rig, who=STANDARD)

        assert view.state == "succeeded", view.outcome
        with pytest.raises(ResourceNotFound):
            await rig.fresh().swarm_actions.deploy(PEER, ENV_A, NAME)

    async def test_the_final_spec_of_a_standard_user_is_checked_again_just_before_it_is_sent(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig, who=STANDARD)
        monkeypatch.setattr(actions_module, "find_restricted_service", lambda spec: ["found"])

        view = await deploy_stack(rig, who=STANDARD)

        assert view.state == "failed" and view.outcome.reason == "restricted", view.outcome
        assert stack_daemon.services == {}

    async def test_a_volume_only_namespace_cannot_be_taken_by_naming_it_and_mounting_its_data(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        stack_daemon.add_volume("db_data", labels={NAMESPACE_LABEL: "db"})
        await control(swarm_services(rig).access, stack_key("db"), administrators_only=True)
        mounting = simple_file(
            deploy={"placement": {"constraints": [f"node.id=={MANAGER}"]}},
            volumes=[{"type": "volume", "source": "data", "target": "/loot"}],
        )

        with pytest.raises(StackNameInUse):
            await rig.fresh().swarm_actions.create(
                STANDARD, ENV_A, name="db", compose_text=mounting, variables=[]
            )

        assert not await can(rig, STANDARD, volume_key("db_data"), "write")


class TestCredentials:
    async def test_registry_credentials_follow_the_header_rules_and_are_bound(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        set_id = await register(rig, "only")
        text = simple_file(image=REFERENCE, deploy={"replicas": 1})
        await create_stack(rig, text, [])

        view = await deploy_stack(rig)

        assert view.state == "succeeded", view.outcome
        headers = dict(stack_daemon.auth_headers)
        assert decode_auth(headers["create"] or "")["username"] == "user-only"
        [binding] = await binding_rows(rig.db)
        assert (binding["image_reference"], binding["registry_credential_id"]) == (
            REFERENCE_KEY,
            set_id,
        )
        spec = next(iter(stack_daemon.services.values()))["Spec"]["TaskTemplate"]["ContainerSpec"]
        assert spec["Image"].endswith(stack_daemon.digest)

        stack_daemon.auth_headers.clear()
        stack_daemon.registry_auth_from.clear()
        same = await redeploy_stack(rig)
        assert same.state == "succeeded" and same.outcome.result.updated == ()
        assert [h for k, h in stack_daemon.auth_headers if k == "update"] == []

        stack_daemon.digest = "sha256:" + "e" * 64
        stack_daemon.auth_headers.clear()
        moved = await redeploy_stack(rig)
        assert moved.outcome.result.updated == ("service:Shop_web",)
        update_headers = [h for k, h in stack_daemon.auth_headers if k == "update"]
        assert len(update_headers) == 1
        assert decode_auth(update_headers[0] or "")["username"] == "user-only"
        assert stack_daemon.registry_auth_from == [None]

        await rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=simple_file(deploy={"replicas": 1})
        )
        stack_daemon.auth_headers.clear()
        changed = await redeploy_stack(rig)
        assert changed.state == "succeeded", changed.outcome
        assert [h for k, h in stack_daemon.auth_headers if k == "update"] == [ANONYMOUS_AUTH]
        every_row = json.dumps(await rows(rig), default=str)
        assert "user-only" not in every_row and (headers["create"] or "x") not in every_row


class TestSecretsStayOut:
    async def test_the_secret_value_is_in_no_row_log_job_view_repr_or_file(
        self,
        swarm_stack_rig: Rig,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        rig = swarm_stack_rig
        caplog.set_level("DEBUG")
        await create_stack(rig)
        view = await deploy_stack(rig)
        await redeploy_stack(rig)
        await rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=shop_variables(secret=SECRET_VALUE + "x")
        )
        await redeploy_stack(rig)
        _, plan = await reviewed_plan(rig)

        assert SECRET_VALUE not in await stack_support.activity_dump(rig)
        assert SECRET_VALUE not in repr(view) + repr(view.outcome) + repr(plan)
        assert SECRET_VALUE not in repr(plan.translation.files)
        assert SECRET_VALUE not in stack_support.logged_text(caplog)
        assert stack_support.files_containing(rig.data_dir.parent, SECRET_VALUE) == []
        assert stack_support.logged_text(caplog)


class TestLocks:
    async def test_a_member_held_by_another_page_makes_the_stack_busy(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        holder = LockHolder(UserActor.of(ADMIN), "admin", "scale")
        service_key = ObjectKey(ENV_A, "service", next(iter(stack_daemon.services)))

        async with rig.engine.locks.hold(service_key, holder):
            view = await redeploy_stack(rig)

        assert view.state == "failed" and view.outcome.reason == "object_busy", view.outcome

    async def test_a_held_stack_refuses_the_other_stack_operations(
        self, swarm_stack_rig: Rig
    ) -> None:
        rig = swarm_stack_rig
        created = await create_stack(rig)
        holder = LockHolder(UserActor.of(ADMIN), "admin", "redeploy")

        async with rig.engine.locks.hold(stack_lock_key(), holder):
            with pytest.raises(ObjectBusy):
                await rig.fresh().swarm_actions.forget(
                    ADMIN, ENV_A, NAME, stack_id=created.stack_id
                )
            busy = await rig.fresh().swarm_planner.busy_error(ADMIN, ENV_A, NAME)

        assert busy is not None and busy.holder.action == "redeploy"
        assert await rig.fresh().swarm_planner.busy_error(ADMIN, ENV_A, NAME) is None

    async def test_a_busy_refusal_recorded_before_a_job_starts_carries_the_object_busy_reason(
        self, swarm_stack_rig: Rig
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        holder = LockHolder(UserActor.of(ADMIN), "admin", "redeploy")
        async with rig.engine.locks.hold(stack_lock_key(), holder):
            busy = await rig.fresh().swarm_planner.busy_error(ADMIN, ENV_A, NAME)
        assert busy is not None

        with pytest.raises(ObjectBusy):
            await rig.fresh().swarm_actions.record_refusal(
                ADMIN, ENV_A, NAME, operation="redeploy", error=busy
            )

        [row] = await rows(rig, "stack_redeployed")
        assert row["detail"]["reason"] == "object_busy"

    async def test_a_running_redeploy_holds_the_stack_and_its_members(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        await deploy_stack(rig)
        await rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(web_replicas=3)
        )
        stack_daemon.gate = asyncio.Event()
        task = asyncio.create_task(redeploy_stack(rig))
        await asyncio.wait_for(stack_daemon.arrived.wait(), 10)

        stack_held = rig.engine.locks.holder_of(stack_lock_key()) is not None
        member_held = [
            rig.engine.locks.holder_of(ObjectKey(ENV_A, "service", sid)) is not None
            for sid in stack_daemon.services
        ]
        stack_daemon.gate.set()
        await task

        assert stack_held and any(member_held)


class TestInterruption:
    async def test_a_cancelled_deploy_finishes_and_writes_an_interrupted_row(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        stack_daemon.gate = asyncio.Event()
        task = asyncio.create_task(rig.fresh().swarm_actions.deploy(ADMIN, ENV_A, NAME, sink=None))
        await asyncio.wait_for(stack_daemon.arrived.wait(), 10)
        task.cancel()
        stack_daemon.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain_background()

        assert stack_daemon.service_names() == ["Shop_api", "Shop_web"]
        [row] = await rows(rig, "stack_deployed")
        assert row["result"] == "success" and row["detail"]["interrupted"] is True, row
        stored = present(await rig.db.fetch_one("SELECT status FROM stack"))
        assert stored["status"] == "active"
        assert rig.engine.locks.holder_of(stack_lock_key()) is None

    async def test_a_cancel_before_the_first_write_leaves_no_row(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        task = asyncio.create_task(rig.fresh().swarm_actions.deploy(ADMIN, ENV_A, NAME, sink=None))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await drain_background()

        assert await rows(rig, "stack_deployed") == []
        assert stack_daemon.mutating() == []
        assert rig.engine.locks.holder_of(stack_lock_key()) is None

    async def test_a_deploy_past_its_deadline_is_outcome_unknown(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        rig.compose.timeout_seconds = SHORT_DEADLINE_SECONDS
        monkeypatch.setattr(actions_module, "DEADLINE_SLACK_SECONDS", 0.0)
        stack_daemon.gate = asyncio.Event()

        with pytest.raises(Exception) as raised:
            await rig.fresh().swarm_actions.deploy(ADMIN, ENV_A, NAME, sink=None)
        stack_daemon.gate.set()

        assert getattr(raised.value, "reason", None) == "outcome_unknown", raised.value
        [row] = await rows(rig, "stack_deployed")
        assert row["detail"]["reason"] == "outcome_unknown"
        stored = present(await rig.db.fetch_one("SELECT status, last_outcome FROM stack"))
        assert tuple(stored) == ("error", "outcome_unknown")

    async def test_a_request_cancelled_past_the_settle_window_is_unknown_then_settled_late(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = swarm_stack_rig
        await create_stack(rig)
        monkeypatch.setattr(actions_module, "SETTLE_MARGIN_SECONDS", SETTLE_BEYOND_THE_WINDOW)
        stack_daemon.gate = asyncio.Event()
        task = asyncio.create_task(rig.fresh().swarm_actions.deploy(ADMIN, ENV_A, NAME, sink=None))
        await asyncio.wait_for(stack_daemon.arrived.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        unknown = await rows(rig, "stack_deployed")
        stored = present(await rig.db.fetch_one("SELECT last_outcome FROM stack"))
        held = rig.engine.locks.holder_of(stack_lock_key())

        stack_daemon.gate.set()
        await drain_background()

        assert [r["detail"].get("reason") for r in unknown] == ["outcome_unknown"]
        assert stored["last_outcome"] == "outcome_unknown" and held is not None
        settled = await rows(rig, "stack_deployed")
        assert [r["result"] for r in settled] == ["failure", "success"], settled
        assert settled[1]["detail"]["settled_late"] is True and settled[1]["detail"]["interrupted"]
        final = present(await rig.db.fetch_one("SELECT status FROM stack"))
        assert final["status"] == "active"
        assert rig.engine.locks.holder_of(stack_lock_key()) is None
