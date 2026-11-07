"""Adopting a Compose project found on the host, deploying it, and forgetting a stack."""

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ControlSpec
from app.services.stack_deploy import ChangedSinceReview, StackNameInUse, StackRejected
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, stack_key
from tests.image_support import REFERENCE, register
from tests.stack_support import Rig, adopt_then_redeploy, compose_doc, project_box
from tests.support import present
from tests.swarm_stack_support import StackDaemon, seed_swarm_stack, simple_file

PROJECT = "ext"
TWO_SERVICES = """
services:
  web:
    image: nginx:1
  db:
    image: nginx:1
"""
APP_FILE = f"""
services:
  app:
    image: {REFERENCE}
"""
UNKNOWN_PROJECT = "nothere"
STALE_ID_OFFSET = 1
CONTAINERS_ON_HOST = 2


async def test_adopting_stores_the_project_under_a_control_without_touching_the_host(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    web = project_box(rig.host, 1, "Ext", "web")
    db = project_box(rig.host, 2, "Ext", "db", state="exited")
    mutations = rig.host.mutations()

    with pytest.raises(PermissionDenied):
        await rig.fresh().actions.adopt(STANDARD, ENV_A, name=PROJECT, compose_text=TWO_SERVICES)
    with pytest.raises(StackRejected) as unknown:
        await rig.fresh().actions.adopt(
            ADMIN, ENV_B, name=UNKNOWN_PROJECT, compose_text=TWO_SERVICES
        )
    adopted = await rig.fresh().actions.adopt(ADMIN, ENV_B, name=PROJECT, compose_text=TWO_SERVICES)
    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.adopt(ADMIN, ENV_B, name=PROJECT, compose_text=TWO_SERVICES)
    detail = await rig.fresh().queries.detail(ADMIN, ENV_B, PROJECT)
    unmanaged = await rig.fresh().queries.unmanaged(ADMIN, ENV_B)

    assert list(unknown.value.fields) == ["name"]
    assert (adopted.containers, adopted.control_created) == (CONTAINERS_ON_HOST, True)
    assert (detail.row.origin, detail.row.status) == ("adopted", "partial")
    assert (detail.row.last_operation, detail.row.last_outcome) == ("adopt", "success")
    assert rig.host.mutations() == mutations and set(rig.host.boxes) == {web.id, db.id}
    cached = present(
        await rig.db.fetch_one(
            "SELECT sub_resource_ids FROM resource_control WHERE resource_type = 'stack'"
        )
    )
    assert f"container:{web.id}" in cached["sub_resource_ids"]
    assert f"container:{db.id}" in cached["sub_resource_ids"]
    assert unmanaged == ()
    rows = await rig.rows("stack_adopted")
    assert [r["result"] for r in rows] == ["failure", "success", "failure"]
    assert rows[1]["detail"]["containers"] == CONTAINERS_ON_HOST
    assert rows[1]["detail"]["deploy_requested"] is False


async def test_adopting_keeps_a_control_that_already_governs_the_name(stack_rig: Rig) -> None:
    rig = stack_rig
    project_box(rig.host, 1, PROJECT)
    await rig.access.controls.create(
        ADMIN,
        stack_key(PROJECT, ENV_B),
        ControlSpec("read", owner_user_id=STANDARD.user_id),
        expected_digest="",
    )

    adopted = await rig.fresh().actions.adopt(
        ADMIN, ENV_B, name=PROJECT, compose_text=compose_doc()
    )

    assert adopted.control_created is False
    kept = present(
        await rig.db.fetch_one(
            "SELECT owner_user_id, access_level FROM resource_control WHERE resource_id = ?",
            (PROJECT,),
        )
    )
    assert (kept["owner_user_id"], kept["access_level"]) == (STANDARD.user_id, "read")


async def test_forgetting_removes_the_record_and_leaves_the_containers_running(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, "Ext")
    adopted = await rig.fresh().actions.adopt(
        ADMIN, ENV_B, name=PROJECT, compose_text=compose_doc()
    )
    review = await rig.fresh().queries.forget_confirmation(ADMIN, ENV_B, PROJECT)
    mutations = rig.host.mutations()

    with pytest.raises(ResourceNotFound):
        await rig.fresh().actions.forget(STANDARD, ENV_B, PROJECT)
    with pytest.raises(ChangedSinceReview):
        await rig.fresh().actions.forget(
            ADMIN, ENV_B, PROJECT, expected_stack_id=adopted.stack_id + STALE_ID_OFFSET
        )
    await rig.fresh().actions.forget(ADMIN, ENV_B, PROJECT, expected_stack_id=review.stack_id)
    unmanaged = await rig.fresh().queries.unmanaged(ADMIN, ENV_B)

    assert (review.stack_id, review.containers) == (adopted.stack_id, 1)
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    assert (
        await rig.db.fetch_one("SELECT 1 FROM resource_control WHERE resource_type='stack'") is None
    )
    assert list(rig.compose.files.base.iterdir()) == []
    assert rig.host.mutations() == mutations and len(rig.host.boxes) == 1
    assert [p.name for p in unmanaged] == [PROJECT]
    forgotten = await rig.rows("stack_forgotten")
    assert [(r["result"], r["detail"].get("reason")) for r in forgotten] == [
        ("failure", "changed_since_review"),
        ("success", None),
    ]


async def test_adopting_with_a_deploy_asks_for_a_credential_choice_before_it_stores_anything(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, PROJECT)
    await register(rig, "first", environments=(ENV_B,))
    second = await register(rig, "second", environments=(ENV_B,))
    choice = {REFERENCE: CredentialChoice(second)}
    queries = rig.fresh().queries

    plain = await queries.validate(
        ADMIN, ENV_B, name=PROJECT, compose_text=APP_FILE, variables=None, adopt=True
    )
    with pytest.raises(StackRejected) as needed:
        await queries.validate(
            ADMIN,
            ENV_B,
            name=PROJECT,
            compose_text=APP_FILE,
            variables=None,
            adopt=True,
            deploy=True,
        )
    offers = await queries.credential_offers(ADMIN, ENV_B, list(needed.value.fields))
    chosen = await queries.validate(
        ADMIN,
        ENV_B,
        name=PROJECT,
        compose_text=APP_FILE,
        variables=None,
        adopt=True,
        deploy=True,
        choices=choice,
    )
    with pytest.raises(StackRejected):
        await adopt_then_redeploy(
            rig.fresh().actions, ADMIN, ENV_B, name=PROJECT, compose_text=APP_FILE
        )

    assert plain.to_pull == (REFERENCE,) and chosen.name == PROJECT
    assert (needed.value.reason, list(needed.value.fields)) == (
        "credential_choice_required",
        [REFERENCE],
    )
    assert len(offers) == 1 and len(offers[0].candidates) == 2 and offers[0].preselected is None
    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None
    failed = (await rig.rows("stack_adopted"))[0]
    assert failed["detail"]["reason"] == "credential_choice_required"


async def test_adopt_validation_refuses_a_standard_user_an_unknown_project_and_a_managed_one(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, "Ext")
    queries = rig.fresh().queries

    with pytest.raises(PermissionDenied):
        await queries.validate(
            STANDARD, ENV_A, name=PROJECT, compose_text=compose_doc(), variables=None, adopt=True
        )
    with pytest.raises(StackRejected) as unknown:
        await queries.validate(
            ADMIN,
            ENV_B,
            name=UNKNOWN_PROJECT,
            compose_text=compose_doc(),
            variables=None,
            adopt=True,
        )
    await rig.fresh().actions.adopt(ADMIN, ENV_B, name=PROJECT, compose_text=compose_doc())
    with pytest.raises(StackNameInUse):
        await queries.validate(
            ADMIN, ENV_B, name=PROJECT, compose_text=compose_doc(), variables=None, adopt=True
        )

    assert list(unknown.value.fields) == ["name"]


async def test_adopt_and_deploy_takes_the_project_over_and_removing_orphans_needs_a_review(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    project_box(rig.host, 1, PROJECT, "web")
    orphan = project_box(rig.host, 2, PROJECT, "old")

    adopted = await adopt_then_redeploy(
        rig.fresh().actions, ADMIN, ENV_B, name=PROJECT, compose_text=compose_doc()
    )
    orphan_survived = orphan.id in rig.host.boxes
    review = await rig.fresh().queries.redeploy_confirmation(ADMIN, ENV_B, PROJECT)
    before = rig.host.mutations()
    with pytest.raises(StackRejected) as unreviewed:
        await rig.fresh().actions.redeploy(ADMIN, ENV_B, PROJECT, remove_orphans=True)
    after_refusal = rig.host.mutations()
    cleaned = await rig.fresh().actions.redeploy(
        ADMIN,
        ENV_B,
        PROJECT,
        remove_orphans=True,
        expected_file_digest=review.expected_file_digest,
        expected_containers=review.expected_containers,
    )

    assert (adopted.services_running, orphan_survived) == (CONTAINERS_ON_HOST, True)
    assert [c.service for c in review.orphans] == ["old"]
    assert list(unreviewed.value.fields) == ["expected_containers"]
    assert after_refusal == before
    assert cleaned.orphans_removed == 1 and orphan.id not in rig.host.boxes
    row = (await rig.rows("stack_adopted"))[0]
    assert row["detail"]["deploy_requested"] is True
    assert [r["result"] for r in await rig.rows("stack_redeployed")] == [
        "success",
        "failure",
        "success",
    ]


NAMESPACE_LABEL = "com.docker.stack.namespace"
NAMESPACE = "Ext"


async def test_a_compose_project_cannot_be_adopted_while_a_swarm_namespace_folds_to_its_name(
    compose_on_swarm_rig: Rig, stack_daemon: StackDaemon
) -> None:
    rig = compose_on_swarm_rig
    project_box(rig.host, 1, "ext")
    stack_daemon.add_config("Ext_cfg", labels={NAMESPACE_LABEL: NAMESPACE})

    with pytest.raises(StackNameInUse):
        await rig.fresh().actions.adopt(ADMIN, ENV_A, name=PROJECT, compose_text=compose_doc())

    assert await rig.db.fetch_one("SELECT 1 FROM stack") is None


async def test_forgetting_a_compose_stack_keeps_its_control_while_a_namespace_still_folds_to_it(
    compose_on_swarm_rig: Rig, stack_daemon: StackDaemon
) -> None:
    rig = compose_on_swarm_rig
    project_box(rig.host, 1, "ext")
    adopted = await rig.fresh().actions.adopt(
        ADMIN, ENV_A, name=PROJECT, compose_text=compose_doc()
    )
    stack_daemon.add_config("Ext_cfg", labels={NAMESPACE_LABEL: NAMESPACE})

    await rig.fresh().actions.forget(ADMIN, ENV_A, PROJECT, expected_stack_id=adopted.stack_id)

    controls = await rig.db.fetch_limited(
        "SELECT resource_id FROM resource_control WHERE resource_type = 'stack'", (), limit=5
    )
    assert [c["resource_id"] for c in controls] == [PROJECT]
    [forgotten] = await rig.rows("stack_forgotten")
    assert forgotten["detail"]["control_kept"] is True
    assert forgotten["detail"]["control_released"] is False


async def test_a_compose_project_folding_to_a_managed_swarm_stack_stays_in_the_unmanaged_list(
    compose_on_swarm_rig: Rig,
) -> None:
    rig = compose_on_swarm_rig
    project_box(rig.host, 1, "ext")
    await seed_swarm_stack(rig, simple_file(), name=NAMESPACE)

    unmanaged = await rig.fresh().queries.unmanaged(ADMIN, ENV_A)

    assert [p.name for p in unmanaged] == [PROJECT]
