"""Swarm stack namespaces: grouping, case folding, visibility and control inheritance."""

from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.repositories.stack import StackRepository
from app.services.access import ResourceKey
from app.services.stack_deploy import file_digest
from tests.access_support import ADMIN, ENV_A, PEER, STANDARD, control
from tests.docker_engine.container_engine import swarm_network_id
from tests.docker_engine.swarm_engine import plain_spec
from tests.services.conftest import SwarmRig
from tests.stack_support import Rig, seed_stack_row
from tests.swarm_stack_support import (
    NAME,
    StackDaemon,
    create_stack,
    deploy_stack,
    reviewed_plan,
    seed_swarm_stack,
    swarm_services,
)
from tests.swarm_support import NAMESPACE_LABEL

STACK_KEY = ResourceKey(ENV_A, "stack", "Shop")


@pytest.fixture
def shop(swarm: SwarmRig) -> dict[str, str]:
    """A Shop stack spelled three ways, one other stack and an unrelated config."""
    host = swarm.host
    host.add_network(
        "shop_default",
        0,
        "overlay",
        network_id=swarm_network_id(9),
        Scope="swarm",
        Labels={NAMESPACE_LABEL: "Shop"},
    )
    return {
        "config": host.add_config("shop-conf", labels={NAMESPACE_LABEL: "shop"}),
        "secret": host.add_secret("shop-sec", labels={NAMESPACE_LABEL: "Shop"}),
        "service": host.add_service(plain_spec("shop_web", Labels={NAMESPACE_LABEL: "Shop"})),
        "pinned": host.add_config("shop-hidden", labels={NAMESPACE_LABEL: "SHOP"}),
        "plain": host.add_config("plain"),
        "other": host.add_service(plain_spec("other_web", Labels={NAMESPACE_LABEL: "Other"})),
    }


async def test_a_standard_user_sees_nothing_of_a_stack_until_its_control_names_them(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    rows = (await swarm.sw.stacks.page(PEER, ENV_A)).rows
    with pytest.raises(ResourceNotFound):
        await swarm.sw.configs.detail(STANDARD, ENV_A, shop["config"])

    assert rows == ()


async def test_a_stack_control_reaches_every_object_whose_namespace_folds_to_its_key(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    swarm.fresh()

    config = await swarm.sw.configs.detail(STANDARD, ENV_A, shop["config"])
    secret = await swarm.sw.secrets.detail(STANDARD, ENV_A, shop["secret"])
    services = await swarm.sw.services.page(STANDARD, ENV_A)
    configs = await swarm.sw.configs.page(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.configs.detail(STANDARD, ENV_A, shop["plain"])

    assert (config.name, secret.name) == ("shop-conf", "shop-sec")
    assert [r.name for r in services.page.items] == ["shop_web"]
    assert [r.name for r in configs.page.items] == ["shop-conf", "shop-hidden"]


async def test_an_objects_own_control_wins_over_the_stack_control(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    await control(
        swarm.access, ResourceKey(ENV_A, "config", shop["pinned"]), administrators_only=True
    )
    swarm.fresh()

    configs = await swarm.sw.configs.page(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.configs.detail(STANDARD, ENV_A, shop["pinned"])

    assert [r.name for r in configs.page.items] == ["shop-conf"]


async def test_a_services_own_control_wins_over_the_stack_control(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    await control(
        swarm.access, ResourceKey(ENV_A, "service", shop["service"]), administrators_only=True
    )
    swarm.fresh()

    services = await swarm.sw.services.page(STANDARD, ENV_A)

    assert services.page.items == []


async def test_the_administrators_list_groups_namespaces_case_insensitively_with_counts(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    page = await swarm.sw.stacks.page(ADMIN, ENV_A)

    assert [(r.name, r.key, r.services, r.configs, r.secrets, r.networks) for r in page.rows] == [
        ("Other", "other", 1, 0, 0, 0),
        ("Shop", "shop", 1, 2, 1, 1),
    ]
    assert page.rows[1].spellings == ("SHOP", "Shop", "shop")


async def test_a_standard_users_list_shows_only_the_stacks_they_may_see_with_their_own_counts(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    await control(
        swarm.access, ResourceKey(ENV_A, "config", shop["pinned"]), administrators_only=True
    )
    swarm.fresh()

    page = await swarm.sw.stacks.page(STANDARD, ENV_A)

    assert [(r.name, r.services, r.configs, r.secrets, r.networks) for r in page.rows] == [
        ("Shop", 1, 1, 1, 1)
    ]


async def test_the_stack_detail_lists_members_and_hides_the_access_tab_from_a_standard_user(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    swarm.fresh()

    detail = await swarm.sw.stacks.detail(STANDARD, ENV_A, "shop")

    assert detail.name == "Shop" and not detail.can_manage_access
    assert [m.name for m in detail.services.items] == ["shop_web"]


@pytest.mark.parametrize("namespace", ["nothing", "-bad", "a" * 64, "x y", "other"])
async def test_a_namespace_that_is_missing_malformed_or_not_visible_is_not_found(
    swarm: SwarmRig, shop: dict[str, str], namespace: str
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    swarm.fresh()

    with pytest.raises(ResourceNotFound):
        await swarm.sw.stacks.detail(STANDARD, ENV_A, namespace)
    with pytest.raises(ResourceNotFound):
        await swarm.sw.stacks.detail(PEER, ENV_A, "shop")


async def test_the_access_panel_is_for_administrators_and_keys_the_control_by_the_spelling(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    await control(swarm.access, STACK_KEY, users=(STANDARD.user_id,))
    swarm.fresh()

    with pytest.raises(PermissionDenied):
        await swarm.sw.stacks.access_panel(STANDARD, ENV_A, "shop")
    panel = await swarm.sw.stacks.access_panel(ADMIN, ENV_A, "SHOP")

    assert panel.key == STACK_KEY


async def test_saving_a_stack_control_refreshes_the_sub_resources_it_covers(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    members = await swarm.sw.stacks.member_refs(ADMIN, ENV_A, "shop")
    panel = await swarm.sw.stacks.access_panel(ADMIN, ENV_A, "SHOP")
    await control(swarm.access, STACK_KEY, public=True)

    refreshed = await swarm.access.controls.refresh_sub_resources(panel.key, members)

    assert {m.resource_type for m in members} == {"service", "config", "secret", "network"}
    assert refreshed
    assert shop["other"] in swarm.host.services


async def test_the_access_context_is_read_before_anything_is_written(
    swarm: SwarmRig, shop: dict[str, str]
) -> None:
    context = await swarm.sw.stacks.access_context(ADMIN, ENV_A, "shop")

    assert context.panel.key == STACK_KEY
    assert {m.resource_type for m in context.members} == {"service", "config", "secret", "network"}


MANAGED_NAME = "Shop"
OTHER_NAMESPACE = "Other"
TWIN = "Twin"
HIDDEN_FROM_STANDARD = {"Other"}


class TestManagedStacks:
    async def listed(self, rig: Rig, who: Any = ADMIN) -> dict[str, Any]:
        page = await swarm_services(rig).stacks.page(who, ENV_A)
        return {row.name: row for row in page.rows}

    async def test_managed_and_unmanaged_namespaces_are_merged_by_folded_name(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_service(plain_spec("shop_web", Labels={NAMESPACE_LABEL: "shop"}))
        stack_daemon.add_service(plain_spec("Other_web", Labels={NAMESPACE_LABEL: OTHER_NAMESPACE}))
        await seed_swarm_stack(swarm_stack_rig, name=MANAGED_NAME)

        rows = await self.listed(swarm_stack_rig)

        assert set(rows) == {OTHER_NAMESPACE, MANAGED_NAME}
        assert (rows[MANAGED_NAME].managed, rows[MANAGED_NAME].adoptable) == (True, False)
        assert rows[MANAGED_NAME].services == 1
        assert (rows[OTHER_NAMESPACE].managed, rows[OTHER_NAMESPACE].adoptable) == (False, True)

    async def test_a_managed_stack_with_no_objects_yet_is_listed_from_its_row(
        self, swarm_stack_rig: Rig
    ) -> None:
        await seed_swarm_stack(swarm_stack_rig, name=MANAGED_NAME)

        rows = await self.listed(swarm_stack_rig)

        assert rows[MANAGED_NAME].managed and rows[MANAGED_NAME].services == 0

    async def test_a_standard_user_sees_only_the_managed_stacks_their_control_reaches(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_service(plain_spec("Other_web", Labels={NAMESPACE_LABEL: OTHER_NAMESPACE}))
        await seed_swarm_stack(swarm_stack_rig, name=MANAGED_NAME, owner=STANDARD.user_id)
        await seed_swarm_stack(swarm_stack_rig, name="Hidden")

        rows = await self.listed(swarm_stack_rig, STANDARD)

        assert set(rows) == {MANAGED_NAME}

    async def test_a_namespace_folding_to_a_compose_stack_names_it_and_cannot_be_adopted(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        rig = swarm_stack_rig
        repository = StackRepository(rig.db, rig.registry.vault, rig.clock)
        await repository.insert(
            environment_id=ENV_A,
            name=TWIN.lower(),
            kind="compose",
            origin="created",
            project_directory=rig.compose.files.new_token(),
            compose_file="services: {}",
            compose_file_digest=file_digest("services: {}"),
            created_by_user_id=None,
        )
        stack_daemon.add_config("c", labels={NAMESPACE_LABEL: TWIN})

        rows = await self.listed(rig)
        detail = await swarm_services(rig).stacks.detail(ADMIN, ENV_A, TWIN)

        assert rows[TWIN].compose_stack == TWIN.lower() and not rows[TWIN].adoptable
        assert detail.compose_stack == TWIN.lower() and not detail.can_adopt
        with pytest.raises(ResourceNotFound):
            await swarm_services(rig).stacks.adopt_context(ADMIN, ENV_A, TWIN)

    async def test_a_managed_stack_has_a_detail_and_an_unmanaged_namespace_has_none(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_service(plain_spec("Shop_web", Labels={NAMESPACE_LABEL: MANAGED_NAME}))
        stack_daemon.add_service(plain_spec("Other_web", Labels={NAMESPACE_LABEL: OTHER_NAMESPACE}))
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        queries = swarm_services(swarm_stack_rig).stacks

        managed = await queries.managed_detail(STANDARD, ENV_A, "SHOP")
        unmanaged = await queries.managed_detail(ADMIN, ENV_A, OTHER_NAMESPACE)

        assert managed is not None and managed.can_write
        assert {s.name: s.deployed for s in managed.services} == {
            "Shop_api": False,
            "Shop_web": True,
        }
        assert unmanaged is None

    async def test_a_declared_service_hidden_by_its_own_control_is_counted_not_listed_as_undeployed(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_service(plain_spec("Shop_web", Labels={NAMESPACE_LABEL: MANAGED_NAME}))
        hidden_id = stack_daemon.add_service(
            plain_spec("Shop_api", Labels={NAMESPACE_LABEL: MANAGED_NAME})
        )
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        await control(
            swarm_stack_rig.access,
            ResourceKey(ENV_A, "service", hidden_id),
            administrators_only=True,
        )

        detail = await swarm_services(swarm_stack_rig).stacks.managed_detail(
            STANDARD, ENV_A, MANAGED_NAME
        )

        assert detail is not None
        assert [s.name for s in detail.services] == ["Shop_web"]
        assert detail.hidden_members == 1

    async def test_the_access_panel_works_from_the_row_before_any_object_exists(
        self, swarm_stack_rig: Rig
    ) -> None:
        await seed_swarm_stack(swarm_stack_rig, name=MANAGED_NAME)
        queries = swarm_services(swarm_stack_rig).stacks

        panel = await queries.access_panel(ADMIN, ENV_A, "shop")
        context = await queries.access_context(ADMIN, ENV_A, "SHOP")

        assert panel.name == MANAGED_NAME
        assert context.members == ()


class TestConfirmations:
    async def test_the_redeploy_confirmation_is_bound_to_the_file_and_the_plan(
        self, swarm_stack_rig: Rig
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        queries = swarm_services(swarm_stack_rig).stacks
        row, plan = await reviewed_plan(swarm_stack_rig)

        review = await queries.redeploy_confirmation(ADMIN, ENV_A, "shop")

        assert review.expected_file_digest == row.compose_file_digest
        assert review.expected_plan == plan.digest
        assert [line.name for line in review.services_update] == ["Shop_api", "Shop_web"]

    async def test_the_remove_confirmation_names_every_member_and_matches_the_plan_digest(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        await create_stack(swarm_stack_rig)
        await deploy_stack(swarm_stack_rig)
        stack_daemon.add_volume("Shop_data", labels={NAMESPACE_LABEL: MANAGED_NAME})
        queries = swarm_services(swarm_stack_rig).stacks
        row = await swarm_stack_rig.fresh().swarm_planner.require_stack(ADMIN, ENV_A, NAME, "write")
        plan = await swarm_stack_rig.fresh().swarm_planner.remove_plan(ADMIN, ENV_A, row)

        review = await queries.remove_confirmation(ADMIN, ENV_A, "SHOP")

        assert review.expected_members == plan.digest
        assert [m.name for m in review.services.items] == ["Shop_api", "Shop_web"]
        assert review.volumes_kept == ("Shop_data",)
        assert review.unwritable_members == 0

    async def test_the_forget_confirmation_is_for_administrators_only(
        self, swarm_stack_rig: Rig
    ) -> None:
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        queries = swarm_services(swarm_stack_rig).stacks

        confirmation = await queries.forget_confirmation(ADMIN, ENV_A, MANAGED_NAME)

        assert confirmation.stack == MANAGED_NAME
        with pytest.raises(PermissionDenied):
            await queries.forget_confirmation(STANDARD, ENV_A, MANAGED_NAME)

    async def test_the_adopt_and_edit_contexts_describe_the_namespace_and_the_stored_stack(
        self, swarm_stack_rig: Rig, stack_daemon: StackDaemon
    ) -> None:
        stack_daemon.add_service(plain_spec("Other_web", Labels={NAMESPACE_LABEL: OTHER_NAMESPACE}))
        await seed_swarm_stack(swarm_stack_rig, owner=STANDARD.user_id)
        queries = swarm_services(swarm_stack_rig).stacks

        adopt = await queries.adopt_context(ADMIN, ENV_A, OTHER_NAMESPACE)
        edit = await queries.edit_context(STANDARD, ENV_A, MANAGED_NAME)

        assert adopt.name == OTHER_NAMESPACE
        assert edit.stack.name == MANAGED_NAME
        assert sorted(v.name for v in edit.variables) == ["CFGVAR", "SECVAR"]
        assert all(v.value is None for v in edit.variables if v.secret)


async def test_managed_names_list_both_kinds_by_kind_and_text_without_touching_the_host(
    swarm_stack_rig: Rig,
) -> None:
    rig = swarm_stack_rig
    await seed_swarm_stack(rig)
    await seed_stack_row(rig, ENV_A, "blog", "services:\n  web:\n    image: nginx:1\n")
    queries = rig.fresh().queries
    before = len(rig.host.requests)

    everything = await queries.managed_names(ADMIN, ENV_A)
    swarm_only = await queries.managed_names(ADMIN, ENV_A, kind="swarm")
    compose_only = await queries.managed_names(ADMIN, ENV_A, kind="compose")
    found = await queries.managed_names(ADMIN, ENV_A, q=NAME[:2].upper())
    standard = await queries.managed_names(STANDARD, ENV_A)

    assert sorted((s.name, s.kind) for s in everything) == [("Shop", "swarm"), ("blog", "compose")]
    assert [(s.name, s.kind) for s in swarm_only] == [(NAME, "swarm")]
    assert [(s.name, s.kind) for s in compose_only] == [("blog", "compose")]
    assert [s.name for s in found] == [NAME]
    assert standard == ()
    assert len(rig.host.requests) == before
