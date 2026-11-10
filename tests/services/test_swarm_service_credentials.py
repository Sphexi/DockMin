"""Registry credentials, digest pinning and image bindings on service writes."""

import asyncio
import json

import pytest

import app.services.swarm_services as services_module
from app.auth.principal import Principal
from app.services.image_bindings import ImageBindingService
from app.services.pull_credentials import CredentialChoice, CredentialChoiceRequired
from app.services.swarm_service_views import ServiceInvalid
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import STANDARD_ID
from tests.image_support import REFERENCE, REFERENCE_KEY, binding_rows, decode_auth, register
from tests.object_support import drain_background, rows_of
from tests.registry_support import ADDRESS, SECRET, bind_image
from tests.services.conftest import SwarmRig
from tests.swarm_support import EMPTY_AUTH

FORM = {"name": "web", "image": REFERENCE, "mode": "replicated", "replicas": "1"}
PLAIN_FORM = {"name": "plain", "image": "busybox:latest", "mode": "replicated", "replicas": "1"}
SETTLE_SECONDS = 0.05
OTHER_IMAGE = f"{ADDRESS}/app:2"


async def deploy(rig: SwarmRig, who: Principal = ADMIN, **fields: str) -> str:
    created = await rig.sw.service_actions.create(who, ENV_A, {**FORM, **fields})
    rig.fresh()
    return created.service_id


def last_auth(rig: SwarmRig, kind: str) -> str | None:
    return [header for name, header in rig.host.auth_headers if name == kind][-1]


async def edit_values(
    rig: SwarmRig, service_id: str, who: Principal = ADMIN, **changes: str
) -> tuple[dict[str, str], int]:
    offer = await rig.sw.services.edit_offer(who, ENV_A, service_id)
    return {**offer.values, **changes}, offer.version


async def test_one_fitting_set_signs_the_digest_lookup_and_the_create_and_binds_the_image(
    swarm: SwarmRig,
) -> None:
    only = await register(swarm, "only")

    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    [binding] = await binding_rows(swarm.db)
    [row] = await rows_of(swarm.db, "service_created")
    login = decode_auth(last_auth(swarm, "create") or "")
    assert login["username"] == "user-only" and login["serveraddress"] == ADDRESS
    assert last_auth(swarm, "distribution") == last_auth(swarm, "create")
    assert (binding["image_reference"], binding["registry_credential_id"]) == (REFERENCE_KEY, only)
    assert created.digest_pinned
    assert row["detail"]["credential_set_id"] == only and row["detail"]["binding"] == "created"
    assert row["detail"]["anonymous"] is False
    assert [r["detail"] for r in await rows_of(swarm.db, "image_bound")]


async def test_two_fitting_sets_ask_which_before_anything_is_sent(swarm: SwarmRig) -> None:
    first = await register(swarm, "first")
    second = await register(swarm, "second")

    with pytest.raises(CredentialChoiceRequired) as asked:
        await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    [row] = await rows_of(swarm.db, "service_created")
    assert {c.id for c in asked.value.candidates} == {first, second}
    assert row["detail"]["reason"] == "credential_choice_required"
    assert swarm.host.mutating() == [] and swarm.host.auth_headers == []
    assert await binding_rows(swarm.db) == []


async def test_the_chosen_set_is_used_and_an_anonymous_choice_sends_no_login(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "first")
    chosen = await register(swarm, "second")

    await swarm.sw.service_actions.create(
        ADMIN, ENV_A, FORM, credential_choice=CredentialChoice(chosen)
    )
    named = decode_auth(last_auth(swarm, "create") or "")
    swarm.fresh()
    await swarm.sw.service_actions.create(
        ADMIN,
        ENV_A,
        {**FORM, "name": "anon", "image": OTHER_IMAGE},
        credential_choice=CredentialChoice(None),
    )

    assert named["username"] == "user-second"
    assert last_auth(swarm, "create") is None
    assert [b["registry_credential_id"] for b in await binding_rows(swarm.db)] == [chosen]


async def test_the_registry_secret_appears_in_no_row_and_the_header_does_reach_the_engine(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")

    await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    every_row = json.dumps(await rows_of(swarm.db), default=str)
    assert SECRET not in every_row and (last_auth(swarm, "create") or "") not in every_row
    assert SECRET in decode_auth(last_auth(swarm, "create") or "")["password"]


async def test_a_failed_digest_lookup_creates_the_service_unpinned_with_a_warning(
    swarm: SwarmRig,
) -> None:
    swarm.host.distribution_fails = True

    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    spec = swarm.host.services[created.service_id]["Spec"]
    [row] = await rows_of(swarm.db, "service_created")
    assert spec["TaskTemplate"]["ContainerSpec"]["Image"] == REFERENCE
    assert not created.digest_pinned and created.warning
    assert row["detail"]["digest_pinned"] is False


async def test_an_image_id_is_never_looked_up(swarm: SwarmRig) -> None:
    await swarm.sw.service_actions.create(ADMIN, ENV_A, {**FORM, "image": "sha256:" + "a" * 64})

    assert [name for name, _ in swarm.host.auth_headers] == ["create"]


async def test_an_image_that_already_names_a_digest_is_not_looked_up(swarm: SwarmRig) -> None:
    pinned = f"{REFERENCE}@sha256:{'b' * 64}"

    await swarm.sw.service_actions.create(ADMIN, ENV_A, {**FORM, "image": pinned})

    assert [name for name, _ in swarm.host.auth_headers] == ["create"]


async def test_a_standard_user_cannot_rebind_a_reference_by_choosing_another_set(
    swarm: SwarmRig,
) -> None:
    bound = await register(swarm, "bound", users=(STANDARD_ID,))
    other = await register(swarm, "other", users=(STANDARD_ID,))
    await bind_image(swarm.db, bound, ENV_A)

    with pytest.raises(ServiceInvalid) as raised:
        await swarm.sw.service_actions.create(
            STANDARD, ENV_A, FORM, credential_choice=CredentialChoice(other)
        )

    row = (await rows_of(swarm.db, "service_created"))[-1]
    assert list(raised.value.fields) == ["image"] and row["detail"]["reason"] == "restricted"
    assert swarm.host.mutating() == []
    assert (await binding_rows(swarm.db))[0]["registry_credential_id"] == bound


async def test_an_update_that_changes_the_image_signs_with_the_set_chosen_for_the_new_one(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")
    service_id = await deploy(swarm)
    values, version = await edit_values(swarm, service_id, image=OTHER_IMAGE)

    await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, values, expected_version=version
    )

    row = (await rows_of(swarm.db, "service_updated"))[-1]
    assert decode_auth(last_auth(swarm, "update") or "")["username"] == "user-only"
    assert swarm.host.registry_auth_from[-1] is None
    assert row["detail"]["image_changed"] is True and row["detail"]["binding"] == "created"
    assert (await binding_rows(swarm.db))[-1]["image_reference"] == f"{ADDRESS}/app:2"


@pytest.mark.parametrize("actor", [ADMIN, STANDARD], ids=["administrator", "standard"])
async def test_an_image_changing_update_with_no_credentials_sends_the_empty_auth_document(
    swarm: SwarmRig, actor: Principal
) -> None:
    service_id = await deploy(swarm, actor, name="anon", image="busybox:latest")
    values, version = await edit_values(swarm, service_id, actor, image="other.example.net/x:1")

    await swarm.sw.service_actions.update(
        actor, ENV_A, service_id, values, expected_version=version
    )

    assert last_auth(swarm, "update") == EMPTY_AUTH
    assert swarm.host.registry_auth_from[-1] is None


async def test_an_update_that_keeps_the_image_reuses_the_stored_credentials_with_no_header(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")
    service_id = await deploy(swarm)
    values, version = await edit_values(swarm, service_id, command="sleep 9")

    await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, values, expected_version=version
    )

    assert last_auth(swarm, "update") is None
    assert swarm.host.registry_auth_from[-1] == "spec"


async def test_scale_force_update_and_rollback_name_the_stored_credentials_and_bind_nothing(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")
    service_id = await deploy(swarm, replicas="2")
    bound_before = len(await binding_rows(swarm.db))
    detail = await swarm.sw.services.detail(ADMIN, ENV_A, service_id)
    await swarm.sw.service_actions.scale(
        ADMIN, ENV_A, service_id, replicas=3, expected_version=detail.version, expected_replicas=2
    )
    swarm.fresh()
    scale_source = swarm.host.registry_auth_from[-1]
    detail = await swarm.sw.services.detail(ADMIN, ENV_A, service_id)
    await swarm.sw.service_actions.force_update(
        ADMIN, ENV_A, service_id, expected_version=detail.version
    )
    force_source = swarm.host.registry_auth_from[-1]
    swarm.fresh()
    impact = await swarm.sw.services.rollback_impact(ADMIN, ENV_A, service_id)
    await swarm.sw.service_actions.rollback(
        ADMIN, ENV_A, service_id, expected_version=impact.version
    )

    updates = [header for name, header in swarm.host.auth_headers if name == "update"]
    assert (scale_source, force_source, swarm.host.registry_auth_from[-1]) == (
        "spec",
        "spec",
        "previous-spec",
    )
    assert updates == [None, None, None]
    assert len(await binding_rows(swarm.db)) == bound_before


async def test_a_create_cancelled_after_dispatch_records_the_binding_in_the_settled_row(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(swarm.sw.service_actions.create(ADMIN, ENV_A, FORM))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    rows = await rows_of(swarm.db)
    [created] = [r for r in rows if r["action"] == "service_created"]
    assert created["result"] == "success" and created["detail"]["interrupted"] is True
    assert created["detail"]["binding"] == "created" and created["detail"]["control_created"]
    assert any(r["action"] == "image_bound" for r in rows)


async def test_a_create_unfinished_when_the_wait_ends_says_binding_not_recorded_then_binds_late(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(swarm, "only")
    monkeypatch.setattr(services_module, "SETTLE_SECONDS", SETTLE_SECONDS)
    swarm.host.gate = asyncio.Event()
    task = asyncio.create_task(swarm.sw.service_actions.create(ADMIN, ENV_A, FORM))
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    first = [r for r in await rows_of(swarm.db) if r["action"] == "service_created"]
    swarm.host.gate.set()
    await drain_background()

    rows = await rows_of(swarm.db)
    created = [r for r in rows if r["action"] == "service_created"]
    assert first[0]["detail"]["reason"] == "outcome_unknown"
    assert first[0]["detail"]["binding"] == "not_recorded"
    assert [r["result"] for r in created] == ["failure", "success"]
    late = created[1]["detail"]
    assert late["settled_late"] is True and late["binding"] == "created"
    assert any(r["action"] == "image_bound" for r in rows)


async def test_an_update_cancelled_after_dispatch_still_binds_the_new_image(
    swarm: SwarmRig,
) -> None:
    await register(swarm, "only")
    service_id = await deploy(swarm)
    values, version = await edit_values(swarm, service_id, image=OTHER_IMAGE)
    swarm.host.gate = asyncio.Event()
    swarm.host.arrived.clear()
    task = asyncio.create_task(
        swarm.sw.service_actions.update(ADMIN, ENV_A, service_id, values, expected_version=version)
    )
    await asyncio.wait_for(swarm.host.arrived.wait(), 5)

    task.cancel()
    swarm.host.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await drain_background()

    [row] = await rows_of(swarm.db, "service_updated")
    assert row["result"] == "success" and row["detail"]["interrupted"] is True
    assert row["detail"]["binding"] in ("created", "kept", "rebound")
    assert decode_auth(last_auth(swarm, "update") or "")["username"] == "user-only"


async def test_a_binding_that_cannot_be_written_is_reported_not_recorded_and_the_service_stands(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*args: object, **kwargs: object) -> None:
        raise RuntimeError("binding store unavailable")

    monkeypatch.setattr(ImageBindingService, "record_pull", failing)
    await register(swarm, "only")

    created = await swarm.sw.service_actions.create(ADMIN, ENV_A, FORM)

    [row] = await rows_of(swarm.db, "service_created")
    assert created.service_id in swarm.host.services and created.control_created
    assert row["result"] == "success" and row["detail"]["binding"] == "not_recorded"
    assert row["detail"]["control_created"] is True and await binding_rows(swarm.db) == []


async def test_an_update_whose_binding_cannot_be_written_is_reported_not_recorded(
    swarm: SwarmRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(swarm, "only")
    service_id = await deploy(swarm)
    values, version = await edit_values(swarm, service_id, image=OTHER_IMAGE)

    async def failing(*args: object, **kwargs: object) -> None:
        raise RuntimeError("binding store unavailable")

    monkeypatch.setattr(ImageBindingService, "record_pull", failing)
    await swarm.sw.service_actions.update(
        ADMIN, ENV_A, service_id, values, expected_version=version
    )

    row = (await rows_of(swarm.db, "service_updated"))[-1]
    assert row["result"] == "success" and row["detail"]["binding"] == "not_recorded"
