"""Tests for app/services/swarm_stack_plan.py over a fake manager and a Compose that resolves."""

from typing import Any

import pytest
import yaml

from app.auth.errors import ResourceNotFound
from app.db.repositories.stack import StackRepository
from app.services.access import ResourceKey
from app.services.stack_collisions import MANAGER_ONLY, REASON_PORT
from app.services.stack_deploy import PreexistingObjects, StackNameInUse, StackRejected, file_digest
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, container_id, control
from tests.docker_engine.container_engine import Publish, swarm_network_id
from tests.docker_engine.swarm_engine import MANAGER, ServicePort, plain_spec
from tests.stack_support import Rig
from tests.swarm_stack_support import (
    NAME,
    NAMESPACE_LABEL,
    SECRET_VALUE,
    StackDaemon,
    create_stack,
    deploy_stack,
    kept_secret_variables,
    reviewed_plan,
    seed_swarm_stack,
    shop_file,
    shop_variables,
    simple_file,
    swarm_services,
)

LABELS = {NAMESPACE_LABEL: NAME}
OTHER_DIGEST = "sha256:" + "ab" * 32
MOVED_DIGEST = "sha256:" + "e" * 64
ADMIN_VARIABLES = shop_variables()
UNWRITABLE = {"stack": "You cannot change every object in this stack."}


def planner_of(rig: Rig) -> Any:
    return rig.fresh().swarm_planner


def document(**sections: Any) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(shop_file())
    data.update(sections)
    return data


def text_of(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data)


async def plan_of(rig: Rig, stack: Any, **kwargs: Any) -> Any:
    options = {"remove_orphans": False, **kwargs}
    return await planner_of(rig).deploy_plan(ADMIN, ENV_A, stack, **options)


async def rejection(call: Any) -> StackRejected:
    with pytest.raises(StackRejected) as raised:
        await call
    return raised.value


class TestFreshPlan:
    async def test_a_new_stack_creates_everything_and_pins_each_image_once(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)

        plan = await plan_of(swarm_stack_rig, stack)
        again = await plan_of(swarm_stack_rig, stack)

        assert plan.digest == again.digest
        assert {line.action for line in plan.lines} == {"create"}
        assert {(line.kind, line.name) for line in plan.lines if line.kind != "service"} == {
            ("network", "Shop_front"),
            ("network", "Shop_default"),
            ("config", plan.translation.files[0].name),
            ("secret", plan.translation.files[1].name),
        }
        assert all("@sha256:" in service.image for service in plan.services)
        assert plan.unpinned == ()

    async def test_a_reference_whose_digest_cannot_be_looked_up_is_planned_unpinned(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        stack_daemon.distribution_fails = True

        plan = await plan_of(swarm_stack_rig, stack)

        assert set(plan.unpinned) == {"busybox:latest"}
        assert all("@" not in service.image for service in plan.services)

    async def test_the_plan_digest_binds_the_image_targets_and_the_resolved_values(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        first = await plan_of(swarm_stack_rig, stack)

        stack_daemon.digest = OTHER_DIGEST
        moved = await plan_of(swarm_stack_rig, stack)

        assert first.digest != moved.digest

    async def test_the_plan_never_shows_a_secret_value_in_its_text(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)

        plan = await plan_of(swarm_stack_rig, stack)

        shown = repr(plan) + repr(plan.translation) + repr(plan.translation.files)
        assert SECRET_VALUE not in shown


class TestRedeployPlans:
    async def test_an_unchanged_file_plans_no_creation_or_removal_and_updates_each_service(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        assert (await deploy_stack(swarm_stack_rig)).state == "succeeded"
        _, plan = await reviewed_plan(swarm_stack_rig)

        assert [line for line in plan.lines if line.action in ("create", "remove")] == []
        assert {line.action for line in plan.lines if line.kind == "service"} == {"update"}
        assert not plan.image_moves

    async def test_a_changed_config_creates_the_new_one_and_removes_the_old_one_only(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=kept_secret_variables(config="config v2")
        )

        _, plan = await reviewed_plan(swarm_stack_rig)

        actions = {(line.action, line.kind) for line in plan.lines}
        assert ("create", "config") in actions and ("remove", "config") in actions
        assert ("create", "secret") not in actions and ("remove", "secret") not in actions

    async def test_a_secret_given_a_new_value_is_replaced_even_when_the_value_is_identical(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, variables=shop_variables()
        )

        _, plan = await reviewed_plan(swarm_stack_rig)

        actions = {(line.action, line.kind) for line in plan.lines}
        assert ("create", "secret") in actions and ("remove", "secret") in actions
        assert ("create", "config") not in actions

    async def test_a_service_outside_the_file_is_kept_unless_removal_is_ticked(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        stack_daemon.add_service(plain_spec("Shop_old", Labels=LABELS))

        kept = await plan_of(swarm_stack_rig, stack)
        gone = await plan_of(swarm_stack_rig, stack, remove_orphans=True)

        assert any((line.action, line.name) == ("keep", "Shop_old") for line in kept.lines)
        assert any((line.action, line.name) == ("remove", "Shop_old") for line in gone.lines)
        assert kept.digest != gone.digest

    async def test_an_undeclared_network_a_staying_service_uses_is_kept(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        await swarm_stack_rig.fresh().swarm_actions.update(
            ADMIN, ENV_A, NAME, compose_text=shop_file(api=False)
        )

        _, plan = await reviewed_plan(swarm_stack_rig)

        networks = [(x.action, x.name, x.detail) for x in plan.lines if x.kind == "network"]
        assert ("keep", "Shop_default", "used by a service that stays") in networks

    async def test_an_external_network_that_carries_the_namespace_label_is_never_removed(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        text = text_of(
            {
                "services": {"web": {"image": "busybox:latest", "networks": ["outside"]}},
                "networks": {"outside": {"external": True}},
            }
        )
        stack_daemon.add_network(
            "outside",
            5,
            "overlay",
            network_id=swarm_network_id(5),
            Scope="swarm",
            Attachable=True,
            Labels=LABELS,
        )
        stack = await seed_swarm_stack(swarm_stack_rig, text)

        plan = await plan_of(swarm_stack_rig, stack)

        assert [line for line in plan.lines if line.action == "remove"] == []

    async def test_an_adopted_namespace_replaces_its_cli_named_objects_on_the_first_redeploy(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_config("Shop_cfg", b"config v1", LABELS)
        stack_daemon.add_service(plain_spec("Shop_web", Labels=LABELS))
        stack_daemon.add_network(
            "Shop_front", 3, "overlay", network_id=swarm_network_id(3), Scope="swarm", Labels=LABELS
        )
        stack = await seed_swarm_stack(swarm_stack_rig, origin="adopted")

        plan = await plan_of(swarm_stack_rig, stack)

        assert plan.adopted_first_redeploy
        assert any(
            (line.action, line.kind, line.name) == ("remove", "config", "Shop_cfg")
            for line in plan.lines
        )

    async def test_a_service_cannot_change_between_replicated_and_global_mode(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        service_id = stack_daemon.add_service(plain_spec("Shop_web", Labels=LABELS))
        stack_daemon.services[service_id]["Spec"]["Mode"] = {"Global": {}}

        failure = await rejection(plan_of(swarm_stack_rig, stack))

        assert "services.web.deploy.mode" in failure.fields


class TestExistingObjects:
    async def test_a_network_that_exists_with_other_settings_is_refused(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        stack_daemon.add_network(
            "Shop_front",
            7,
            "overlay",
            network_id=swarm_network_id(7),
            Scope="swarm",
            Internal=True,
            Labels=LABELS,
        )

        failure = await rejection(plan_of(swarm_stack_rig, stack))

        assert failure.reason == "network_differs"
        assert list(failure.fields) == ["networks.front"]

    @pytest.mark.parametrize(
        ("stated", "differs"),
        [
            pytest.param({"driver": "custom-ipam"}, True, id="a-stated-ipam-driver-that-differs"),
            pytest.param({"driver": "default"}, False, id="a-stated-ipam-driver-that-matches"),
            pytest.param({}, False, id="an-unstated-ipam-driver"),
        ],
    )
    async def test_a_stated_ipam_driver_is_compared_with_the_one_the_network_holds(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon, stated: dict[str, str], differs: bool
    ) -> None:
        data = document()
        data["networks"] = {"front": {"ipam": stated}}
        stack = await seed_swarm_stack(swarm_stack_rig, text_of(data))
        stack_daemon.add_network(
            "Shop_front",
            7,
            "overlay",
            network_id=swarm_network_id(7),
            Scope="swarm",
            Labels=LABELS,
            IPAM={"Driver": "default", "Config": []},
        )

        if differs:
            failure = await rejection(plan_of(swarm_stack_rig, stack))
            assert failure.reason == "network_differs"
        else:
            assert await plan_of(swarm_stack_rig, stack)

    async def test_an_object_of_the_same_name_without_the_namespace_is_refused_by_name(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        stack_daemon.add_service(plain_spec("Shop_web", Labels={"other": "x"}))

        with pytest.raises(PreexistingObjects) as raised:
            await plan_of(swarm_stack_rig, stack)

        assert "Service Shop_web" in raised.value.objects

    async def test_a_config_whose_content_does_not_match_its_name_is_refused(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig)
        first = await plan_of(swarm_stack_rig, stack)
        config_name = first.translation.files[0].name
        stack_daemon.add_config(config_name, b"someone else's content", LABELS)

        with pytest.raises(PreexistingObjects) as raised:
            await plan_of(swarm_stack_rig, stack)

        assert f"Config {config_name}" in raised.value.objects

    async def test_a_name_held_by_an_object_of_another_namespace_cannot_be_created(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_config("other", labels={NAMESPACE_LABEL: "taken"})

        with pytest.raises(StackNameInUse):
            await planner_of(swarm_stack_rig).validate(
                ADMIN, ENV_A, name="TAKEN", compose_text=shop_file(), variables=ADMIN_VARIABLES
            )


class TestSecretsInConfigs:
    @pytest.mark.parametrize(
        "service",
        [
            pytest.param({"command": ["run", SECRET_VALUE]}, id="command"),
            pytest.param({"labels": {"note": SECRET_VALUE}}, id="label"),
            pytest.param({"healthcheck": {"test": ["CMD", SECRET_VALUE]}}, id="health-check"),
        ],
    )
    async def test_a_secret_value_in_a_reader_visible_string_is_refused_without_echoing_it(
        self, swarm_stack_rig: Rig, service: dict[str, Any]
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig, simple_file(**service))

        failure = await rejection(plan_of(swarm_stack_rig, stack))

        assert failure.reason == "secret_in_config"
        assert SECRET_VALUE not in str(failure.fields)

    async def test_a_config_holding_a_secret_value_is_refused(self, swarm_stack_rig: Rig) -> None:
        data = document(configs={"cfg": {"content": f"pw={SECRET_VALUE}\n"}})
        stack = await seed_swarm_stack(swarm_stack_rig, text_of(data))

        failure = await rejection(plan_of(swarm_stack_rig, stack))

        assert failure.reason == "secret_in_config"
        assert SECRET_VALUE not in str(failure.fields)

    async def test_a_secret_value_in_the_environment_is_allowed(self, swarm_stack_rig: Rig) -> None:
        text = simple_file(environment=[f"T={SECRET_VALUE}"])
        stack = await seed_swarm_stack(swarm_stack_rig, text)

        assert await plan_of(swarm_stack_rig, stack)


class TestStandardUserLayers:
    def hostile(self) -> str:
        data = document()
        data["services"]["web"]["cap_add"] = ["NET_ADMIN"]
        data["services"]["web"]["volumes"] = [
            {"type": "bind", "source": "/etc", "target": "/e", "bind": {}}
        ]
        return text_of(data)

    async def test_the_compose_scan_refuses_a_restricted_file_naming_every_path(
        self, swarm_stack_rig: Rig
    ) -> None:
        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                STANDARD, ENV_A, name="Mine", compose_text=self.hostile(), variables=ADMIN_VARIABLES
            )
        )

        assert failure.reason == "restricted"
        assert {"services.web.cap_add", "services.web.volumes[0].type"} <= set(failure.fields)

    async def test_the_backstop_over_the_final_specs_refuses_what_the_scan_missed(
        self, swarm_stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.services.swarm_stack_plan as module
        from app.services.stack_restrictions import StackScan

        monkeypatch.setattr(module, "scan", lambda project, **kw: StackScan((), (), (), (), {}))

        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                STANDARD, ENV_A, name="Mine", compose_text=self.hostile(), variables=ADMIN_VARIABLES
            )
        )

        assert failure.reason == "restricted"
        assert any("TaskTemplate" in message for message in failure.fields.values())

    async def test_an_administrator_is_not_refused_and_the_option_ids_are_recorded(
        self, swarm_stack_rig: Rig
    ) -> None:
        report = await planner_of(swarm_stack_rig).validate(
            ADMIN, ENV_A, name="Mine", compose_text=self.hostile(), variables=ADMIN_VARIABLES
        )

        assert set(report.restricted) >= {"cap_add", "bind_mount"}

    async def test_a_reserved_label_is_refused_for_everyone(self, swarm_stack_rig: Rig) -> None:
        text = simple_file(labels={NAMESPACE_LABEL: "evil"})

        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                ADMIN, ENV_A, name="Mine", compose_text=text, variables=[]
            )
        )

        assert failure.reason == "restricted"

    async def test_the_replica_ceiling_differs_by_role(self, swarm_stack_rig: Rig) -> None:
        text = simple_file(deploy={"replicas": 500})
        planner = planner_of(swarm_stack_rig)

        failure = await rejection(
            planner.validate(STANDARD, ENV_A, name="Mine", compose_text=text, variables=[])
        )
        accepted = await planner.validate(
            ADMIN, ENV_A, name="Mine", compose_text=text, variables=[]
        )

        assert "services.web.deploy.replicas" in failure.fields
        assert accepted is not None

    async def test_a_key_with_no_swarm_form_is_refused_for_an_administrator_with_its_path(
        self, swarm_stack_rig: Rig
    ) -> None:
        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                ADMIN,
                ENV_A,
                name="Mine",
                compose_text=simple_file(container_name="w"),
                variables=[],
            )
        )

        assert failure.reason == "refused_construct"
        assert "services.web.container_name" in failure.fields

    async def test_a_stored_file_reading_a_disclosing_variable_is_refused_before_a_plan_is_made(
        self, swarm_stack_rig: Rig
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig, simple_file(environment=["DOCKER_HOST"]))

        failure = await rejection(plan_of(swarm_stack_rig, stack))

        assert failure.reason == "refused_construct"
        assert failure.fields == {
            "compose_file": "`services.web.environment[0]`: "
            "the variable DOCKER_HOST is reserved and cannot be used in a stack."
        }

    async def test_external_objects_need_reach_and_an_administrator_resolves_them(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_network("ext", 9, "overlay", network_id=swarm_network_id(9), Scope="swarm")
        stack_daemon.add_config("extcfg")
        stack_daemon.add_secret("extsec")
        data = {
            "services": {
                "api": {
                    "image": "busybox:latest",
                    "networks": ["outer"],
                    "configs": ["shared"],
                    "secrets": ["shared"],
                    "volumes": [{"type": "volume", "source": "data", "target": "/d"}],
                }
            },
            "networks": {"outer": {"external": True, "name": "ext"}},
            "configs": {"shared": {"external": True, "name": "extcfg"}},
            "secrets": {"shared": {"external": True, "name": "extsec"}},
            "volumes": {"data": {}},
        }
        planner = planner_of(swarm_stack_rig)

        failure = await rejection(
            planner.validate(STANDARD, ENV_A, name="Mine", compose_text=text_of(data), variables=[])
        )
        admin = await planner.validate(
            ADMIN, ENV_A, name="Mine", compose_text=text_of(data), variables=[]
        )

        assert failure.reason == "restricted"
        assert [service.name for service in admin.services] == ["api"]

    async def test_a_standard_owner_with_the_manager_pin_and_a_named_volume_is_not_restricted(
        self, swarm_stack_rig: Rig
    ) -> None:
        data = yaml.safe_load(
            simple_file(deploy={"placement": {"constraints": [f"node.id == {MANAGER}"]}})
        )
        data["services"]["web"]["volumes"] = [{"type": "volume", "source": "data", "target": "/d"}]
        data["volumes"] = {"data": {}}
        stack = await seed_swarm_stack(swarm_stack_rig, text_of(data), owner=STANDARD.user_id)

        plan = await planner_of(swarm_stack_rig).deploy_plan(
            STANDARD, ENV_A, stack, remove_orphans=False
        )

        assert plan.restricted == ()

    async def test_a_named_volume_without_the_manager_pin_is_refused_for_a_standard_user(
        self, swarm_stack_rig: Rig
    ) -> None:
        data = yaml.safe_load(simple_file())
        data["services"]["web"]["volumes"] = [{"type": "volume", "source": "data", "target": "/d"}]
        data["volumes"] = {"data": {}}

        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                STANDARD, ENV_A, name="Mine", compose_text=text_of(data), variables=[]
            )
        )

        assert failure.reason == "restricted"


class TestEveryMemberWritable:
    async def test_a_member_with_a_stricter_own_control_blocks_redeploy_and_remove(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        service_id = stack_daemon.add_service(plain_spec("Shop_extra", Labels=LABELS))
        access = swarm_stack_rig.access
        await control(access, ResourceKey(ENV_A, "service", service_id), administrators_only=True)
        planner = planner_of(swarm_stack_rig)

        redeploy = await rejection(
            planner.deploy_plan(STANDARD, ENV_A, stack, remove_orphans=False)
        )
        remove = await rejection(planner.remove_plan(STANDARD, ENV_A, stack))

        assert redeploy.fields == UNWRITABLE
        assert remove.fields == UNWRITABLE

    async def test_a_stack_with_every_member_writable_plans_for_its_owner(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack = await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        stack_daemon.add_service(plain_spec("Shop_extra", Labels=LABELS))

        plan = await planner_of(swarm_stack_rig).remove_plan(STANDARD, ENV_A, stack)

        assert list(plan.members)

    async def test_someone_with_no_access_to_the_stack_finds_it_missing(
        self, swarm_stack_rig: Rig
    ) -> None:
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        planner = planner_of(swarm_stack_rig)

        with pytest.raises(ResourceNotFound):
            await planner.require_stack(PEER, ENV_A, NAME, "read")
        with pytest.raises(ResourceNotFound):
            await planner.require_stack(STANDARD, ENV_A, "nothere", "read")


class TestValidationReport:
    async def test_validating_an_existing_stack_without_variables_keeps_the_stored_ones(
        self, swarm_stack_rig: Rig
    ) -> None:
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)

        report = await planner_of(swarm_stack_rig).validate(
            STANDARD, ENV_A, name="shop", compose_text=shop_file(), variables=None, existing=True
        )

        assert [service.name for service in report.services] == ["api", "web"]

    async def test_adoption_needs_the_namespace_to_exist_and_accepts_it_in_any_case(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_config("c", labels={NAMESPACE_LABEL: "taken"})
        planner = planner_of(swarm_stack_rig)

        missing = await rejection(
            planner.validate(
                ADMIN,
                ENV_A,
                name="Nowhere",
                compose_text=shop_file(),
                variables=ADMIN_VARIABLES,
                adopt=True,
            )
        )
        report = await planner.validate(
            ADMIN,
            ENV_A,
            name="taken",
            compose_text=shop_file(),
            variables=ADMIN_VARIABLES,
            adopt=True,
        )

        assert missing.fields
        assert report.compose_digest == file_digest(shop_file())

    async def test_a_dotted_name_is_refused_before_anything_is_read(
        self, swarm_stack_rig: Rig
    ) -> None:
        failure = await rejection(
            planner_of(swarm_stack_rig).validate(
                ADMIN, ENV_A, name="bad.name", compose_text=shop_file(), variables=ADMIN_VARIABLES
            )
        )

        assert "name" in failure.fields

    async def test_an_image_id_is_neither_looked_up_nor_pinned(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        text = simple_file(image="sha256:" + "a" * 64)

        report = await planner_of(swarm_stack_rig).validate(
            ADMIN, ENV_A, name="Mine", compose_text=text, variables=[], resolve_images=True
        )

        assert report.images


async def test_a_stack_row_inserted_by_another_kind_is_not_a_swarm_stack(
    swarm_stack_rig: Rig,
) -> None:
    rig = swarm_stack_rig
    repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
    await repository.insert(
        environment_id=ENV_A,
        name="plain",
        kind="compose",
        origin="created",
        project_directory=rig.compose.files.new_token(),
        compose_file=simple_file(),
        compose_file_digest=file_digest(simple_file()),
        created_by_user_id=None,
    )

    with pytest.raises(ResourceNotFound):
        await planner_of(rig).require_stack(ADMIN, ENV_A, "plain", "read")


class TestUpdateExemption:
    def file_with_external_network(self) -> str:
        return text_of(
            {
                "services": {"web": {"image": "busybox:latest", "networks": ["outer"]}},
                "networks": {"outer": {"external": True, "name": "ext"}},
            }
        )

    async def test_a_reference_the_stored_service_already_holds_is_exempt_from_the_reach_check(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        external_id = stack_daemon.add_network(
            "ext", 9, "overlay", network_id=swarm_network_id(9), Scope="swarm"
        )
        stack = await seed_swarm_stack(
            swarm_stack_rig, self.file_with_external_network(), owner=STANDARD.user_id
        )
        planner = planner_of(swarm_stack_rig)

        refused = await rejection(planner.deploy_plan(STANDARD, ENV_A, stack, remove_orphans=False))
        stack_daemon.add_service(
            plain_spec(
                "Shop_web",
                Labels=LABELS,
                TaskTemplate={
                    "ContainerSpec": {"Image": "busybox:latest"},
                    "Networks": [{"Target": external_id}],
                },
            )
        )
        allowed = await planner.deploy_plan(STANDARD, ENV_A, stack, remove_orphans=False)

        assert refused.reason == "restricted"
        assert [line.action for line in allowed.lines if line.kind == "service"] == ["update"]


PORT = 8080
HOST_PORT = 9000
OTHER_INGRESS = 9100
OTHER_HOST_MODE = 9200
PORTS_FIELD = "services.web.ports"
PUBLISHED_BY_OTHER = f"Port {PORT}/tcp on every address is already published by the service other."


def host_mode(port: int) -> dict[str, Any]:
    return {"target": 80, "published": port, "mode": "host"}


class TestCollisionChecks:
    async def test_an_ingress_port_another_service_holds_refuses_and_the_stacks_own_do_not(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await seed_swarm_stack(rig)
        other = stack_daemon.published_service("other", [ServicePort(PORT)])
        arguments: dict[str, Any] = {
            "name": NAME,
            "compose_text": simple_file(ports=[f"{PORT}:80"]),
            "variables": ADMIN_VARIABLES,
            "existing": True,
        }

        refused = await rejection(
            planner_of(rig).validate(ADMIN, ENV_A, check_collisions=True, **arguments)
        )
        unchecked = await planner_of(rig).validate(ADMIN, ENV_A, **arguments)
        del stack_daemon.services[other]
        stack_daemon.published_service(
            "Shop_web", [ServicePort(PORT)], labels={NAMESPACE_LABEL: NAME.upper()}
        )
        own = await planner_of(rig).validate(ADMIN, ENV_A, check_collisions=True, **arguments)

        assert refused.reason == REASON_PORT
        assert refused.fields == {PORTS_FIELD: PUBLISHED_BY_OTHER}
        assert str(refused) == f"Service web publishes tcp port {PORT} that is already published."
        assert unchecked.warnings == () and own.warnings == ()

    async def test_host_mode_ports_are_checked_against_ingress_ports_and_the_managers_containers(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        await seed_swarm_stack(rig)
        stack_daemon.add(container_id(1), "batch", publishes=[Publish(HOST_PORT)])
        stack_daemon.published_service("other", [ServicePort(OTHER_INGRESS)])
        stack_daemon.published_service("sidecar", [ServicePort(OTHER_HOST_MODE, mode="host")])

        async def check(*ports: dict[str, Any]) -> Any:
            return await planner_of(rig).validate(
                ADMIN,
                ENV_A,
                name=NAME,
                compose_text=simple_file(ports=list(ports)),
                variables=ADMIN_VARIABLES,
                existing=True,
                check_collisions=True,
            )

        on_the_manager = await check(host_mode(HOST_PORT))
        against_ingress = await rejection(check(host_mode(OTHER_INGRESS)))
        against_host_mode = await check(host_mode(OTHER_HOST_MODE))
        ingress_against_host_mode = await rejection(
            check({"target": 80, "published": OTHER_HOST_MODE})
        )

        assert on_the_manager.warnings == (
            MANAGER_ONLY,
            f"Service web: port {HOST_PORT}/tcp on every address is published on the manager by "
            "the container batch. This conflicts if a task of the service is placed there.",
        )
        assert against_ingress.reason == REASON_PORT
        assert against_host_mode.warnings == (MANAGER_ONLY,)
        assert ingress_against_host_mode.reason == REASON_PORT

    async def test_a_standard_user_sees_another_service_and_a_manager_container_only_generically(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        hidden = stack_daemon.published_service("other", [ServicePort(PORT)])
        stack_daemon.add(container_id(1), "batch", publishes=[Publish(HOST_PORT)])
        await control(rig.access, ResourceKey(ENV_A, "service", hidden), administrators_only=True)
        await control(
            rig.access, ResourceKey(ENV_A, "container", container_id(1)), administrators_only=True
        )

        refused = await rejection(
            planner_of(rig).validate(
                STANDARD,
                ENV_A,
                name="Mine",
                compose_text=simple_file(ports=[f"{PORT}:80"]),
                variables=[],
                check_collisions=True,
            )
        )
        warned = await planner_of(rig).validate(
            STANDARD,
            ENV_A,
            name="Mine",
            compose_text=simple_file(ports=[host_mode(HOST_PORT)]),
            variables=[],
            check_collisions=True,
        )

        assert refused.fields == {
            PORTS_FIELD: f"Port {PORT}/tcp on every address is already published by "
            "another service."
        }
        assert warned.warnings == (
            MANAGER_ONLY,
            "Service web: a container you cannot see on the manager publishes a port that "
            "overlaps the host-mode ports this service publishes.",
        )

    async def test_the_redeploy_confirmation_refuses_a_held_port_and_warns_about_the_manager(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        web = {"ports": [f"{PORT}:80", host_mode(HOST_PORT)]}
        await create_stack(
            rig, text_of({"services": {"web": {"image": "busybox:latest", **web}}}), []
        )
        await deploy_stack(rig)
        stack_daemon.add(container_id(1), "batch", publishes=[Publish(HOST_PORT)])
        taken = stack_daemon.published_service("other", [ServicePort(PORT)])

        refused = await rejection(
            swarm_services(rig).stacks.redeploy_confirmation(
                ADMIN, ENV_A, NAME, check_collisions=True
            )
        )
        del stack_daemon.services[taken]
        review = await swarm_services(rig).stacks.redeploy_confirmation(
            ADMIN, ENV_A, NAME, check_collisions=True
        )
        plain = await swarm_services(rig).stacks.redeploy_confirmation(ADMIN, ENV_A, NAME)

        assert refused.fields == {PORTS_FIELD: PUBLISHED_BY_OTHER}
        assert refused.warnings[0] == MANAGER_ONLY
        assert review.warnings[0] == MANAGER_ONLY and len(review.warnings) == 2
        assert plain.warnings == ()
