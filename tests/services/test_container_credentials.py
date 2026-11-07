"""Container create and recreate pulling through the real credential bindings."""

from typing import Any

import pytest

from app.auth.errors import PermissionDenied
from app.services.container_provisioning import ConfigurationRejected, PullFailed
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoice,
    CredentialChoiceRequired,
)
from tests.access_support import ADMIN, ENV_A, STANDARD, container_id
from tests.auth_support import STANDARD_ID
from tests.docker_engine.container_engine import ENGINE_TEXT
from tests.environment_support import activity_rows
from tests.image_support import (
    REFERENCE,
    REFERENCE_KEY,
    binding_rows,
    decode_auth,
    digest,
    full_id,
    mark_missing,
    register,
    rows_for,
)
from tests.provisioning_support import create_form
from tests.registry_support import ADDRESS, SECRET, bind_image
from tests.services.conftest import ImageRig

OLD = container_id(0xB1)
PULLED_ID = full_id(7)


def registry_form(**fields: str) -> dict[str, str]:
    return create_form(image=REFERENCE, **fields)


def seed_registry_container(rig: ImageRig) -> None:
    rig.host.add_image(REFERENCE, full_id(1), config={"Cmd": ["serve"]})
    rig.host.add_network("appnet", 0xE1)
    rig.host.add_configured(
        OLD,
        "web",
        {
            "Image": REFERENCE,
            "Env": ["APP=1"],
            "HostConfig": {"NetworkMode": "appnet"},
            "NetworkingConfig": {"EndpointsConfig": {f"{0xE1:064x}": {"Aliases": ["web"]}}},
        },
    )
    rig.host.pull_id = PULLED_ID
    rig.host.pull_config = {"Cmd": ["serve"]}


def sent_login(rig: ImageRig) -> dict[str, str]:
    [pull] = rig.host.calls("POST", "/images/create")
    return decode_auth(pull.headers["x-registry-auth"])


async def created_rows(rig: ImageRig) -> list[dict[str, Any]]:
    return [r for r in await activity_rows(rig.db) if r["action"] == "container_created"]


async def recreated_rows(rig: ImageRig) -> list[dict[str, Any]]:
    return [r for r in await activity_rows(rig.db) if r["action"] == "container_recreated"]


async def test_creating_from_an_unbound_image_with_two_fitting_sets_asks_which_and_pulls_nothing(
    image_rig: ImageRig,
) -> None:
    first = await register(image_rig, "first")
    second = await register(image_rig, "second")

    with pytest.raises(CredentialChoiceRequired) as asked:
        await image_rig.containers.creation.create(ADMIN, ENV_A, registry_form())

    assert {c.id for c in asked.value.candidates} == {first, second}
    assert image_rig.host.mutations() == []
    assert await binding_rows(image_rig.db) == []
    [row] = await created_rows(image_rig)
    assert row["detail"]["reason"] == "credential_choice_required"


async def test_the_chosen_set_pulls_the_image_and_is_bound_with_the_container(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True)
    chosen = await register(image_rig, "second")
    image_rig.host.pull_digest = digest(7)

    outcome = await image_rig.containers.creation.create(
        ADMIN, ENV_A, registry_form(), credential_choice=CredentialChoice(chosen)
    )

    [binding] = await binding_rows(image_rig.db)
    [row] = await created_rows(image_rig)
    texts = str(await activity_rows(image_rig.db))
    login = sent_login(image_rig)
    assert (login["username"], login["serveraddress"]) == ("user-second", ADDRESS)
    assert (binding["image_reference"], binding["registry_credential_id"]) == (
        REFERENCE_KEY,
        chosen,
    )
    assert binding["observed_digest"] == digest(7)
    assert row["target_id"] == outcome.container_id
    assert row["detail"]["credential_set_id"] == chosen
    assert row["detail"]["credential_source"] == "choice"
    assert row["detail"]["binding"] == "created"
    assert len(await rows_for(image_rig.db, "image_bound")) == 1
    assert SECRET not in texts
    assert header_sent(image_rig) not in texts


def header_sent(rig: ImageRig) -> str:
    [pull] = rig.host.calls("POST", "/images/create")
    return str(pull.headers["x-registry-auth"])


async def test_a_pull_the_chosen_set_cannot_make_is_tried_once_with_that_set_and_never_with_another(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True)
    chosen = await register(image_rig, "second")
    image_rig.host.pull_status = 401

    with pytest.raises(PullFailed):
        await image_rig.containers.creation.create(
            ADMIN, ENV_A, registry_form(), credential_choice=CredentialChoice(chosen)
        )

    [pull] = image_rig.host.calls("POST", "/images/create")
    assert decode_auth(pull.headers["x-registry-auth"])["username"] == "user-second"
    assert await binding_rows(image_rig.db) == []
    assert image_rig.host.verbs() == ["pull"]


async def test_an_image_whose_create_fails_after_the_pull_is_left_unbound(
    image_rig: ImageRig,
) -> None:
    chosen = await register(image_rig, "chosen")
    image_rig.host.fail("create", 400)

    with pytest.raises(ConfigurationRejected) as failed:
        await image_rig.containers.creation.create(
            ADMIN, ENV_A, registry_form(), credential_choice=CredentialChoice(chosen)
        )

    assert ENGINE_TEXT not in str(failed.value)
    assert image_rig.host.calls("POST", "/images/create") != []
    assert await binding_rows(image_rig.db) == []
    assert await rows_for(image_rig.db, "image_bound") == []


async def test_creating_from_a_bound_image_pulls_with_the_bound_set_not_the_default(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first", default=True)
    bound = await register(image_rig, "second")
    await bind_image(image_rig.db, bound, ENV_A)

    await image_rig.containers.creation.create(ADMIN, ENV_A, registry_form())

    [row] = await created_rows(image_rig)
    assert sent_login(image_rig)["username"] == "user-second"
    assert (row["detail"]["credential_set_id"], row["detail"]["credential_source"]) == (
        bound,
        "binding",
    )
    assert row["detail"]["binding"] == "kept"


async def test_creating_from_an_image_with_one_fitting_set_uses_it_and_binds_it(
    image_rig: ImageRig,
) -> None:
    only = await register(image_rig, "only")

    await image_rig.containers.creation.create(ADMIN, ENV_A, registry_form())

    [binding] = await binding_rows(image_rig.db)
    assert binding["registry_credential_id"] == only
    assert sent_login(image_rig)["username"] == "user-only"


async def test_a_standard_user_cannot_change_the_set_a_reference_is_bound_to_by_creating(
    image_rig: ImageRig,
) -> None:
    bound = await register(image_rig, "bound", users=(STANDARD_ID,))
    other = await register(image_rig, "other", users=(STANDARD_ID,))
    await bind_image(image_rig.db, bound, ENV_A)

    with pytest.raises(PermissionDenied):
        await image_rig.containers.creation.create(
            STANDARD, ENV_A, registry_form(), credential_choice=CredentialChoice(other)
        )

    assert image_rig.host.mutations() == []
    assert (await binding_rows(image_rig.db))[0]["registry_credential_id"] == bound


async def test_an_anonymous_choice_pulls_without_credentials_and_binds_anonymously(
    image_rig: ImageRig,
) -> None:
    await register(image_rig, "first")
    await register(image_rig, "second")

    await image_rig.containers.creation.create(
        ADMIN, ENV_A, registry_form(), credential_choice=CredentialChoice(None)
    )

    [binding] = await binding_rows(image_rig.db)
    [pull] = image_rig.host.calls("POST", "/images/create")
    assert "x-registry-auth" not in pull.headers
    assert (binding["registry_credential_id"], binding["state"]) == (None, "bound")


async def test_recreate_with_a_pull_uses_the_bound_set_when_two_sets_share_the_host(
    image_rig: ImageRig,
) -> None:
    seed_registry_container(image_rig)
    await register(image_rig, "first", default=True)
    bound = await register(image_rig, "second")
    await bind_image(image_rig.db, bound, ENV_A)
    image_rig.host.pull_digest = digest(8)

    outcome = await image_rig.containers.recreation.recreate(
        ADMIN, ENV_A, OLD, expected_name="web", pull=True
    )

    [row] = await recreated_rows(image_rig)
    [binding] = await binding_rows(image_rig.db)
    login = sent_login(image_rig)
    assert (login["username"], login["serveraddress"]) == ("user-second", ADDRESS)
    assert outcome.pulled is True
    assert (row["detail"]["credential_set_id"], row["detail"]["credential_source"]) == (
        bound,
        "binding",
    )
    assert (binding["registry_credential_id"], binding["observed_digest"]) == (bound, digest(8))
    assert SECRET not in str(await activity_rows(image_rig.db))


async def test_recreate_with_a_pull_of_an_unbound_image_and_two_sets_asks_and_changes_nothing(
    image_rig: ImageRig,
) -> None:
    seed_registry_container(image_rig)
    await register(image_rig, "first")
    await register(image_rig, "second")

    with pytest.raises(CredentialChoiceRequired):
        await image_rig.containers.recreation.recreate(
            ADMIN, ENV_A, OLD, expected_name="web", pull=True
        )

    assert OLD in image_rig.host.boxes
    assert image_rig.host.mutations() == []


async def test_recreate_with_a_pull_of_a_credential_missing_image_needs_an_administrators_choice(
    image_rig: ImageRig,
) -> None:
    seed_registry_container(image_rig)
    gone = await register(image_rig, "gone")
    replacement = await register(image_rig, "replacement")
    await bind_image(image_rig.db, gone, ENV_A)
    await mark_missing(image_rig.db, gone)

    with pytest.raises(BindingCredentialMissing):
        await image_rig.containers.recreation.recreate(
            ADMIN, ENV_A, OLD, expected_name="web", pull=True
        )
    await image_rig.containers.recreation.recreate(
        ADMIN,
        ENV_A,
        OLD,
        expected_name="web",
        pull=True,
        credential_choice=CredentialChoice(replacement),
    )

    [binding] = await binding_rows(image_rig.db)
    [rebound] = await rows_for(image_rig.db, "image_rebound")
    assert (binding["state"], binding["registry_credential_id"]) == ("bound", replacement)
    assert rebound["detail"]["previous_state"] == "credential_missing"


async def test_recreate_without_a_pull_touches_no_registry_and_no_binding(
    image_rig: ImageRig,
) -> None:
    seed_registry_container(image_rig)
    await register(image_rig, "first")
    await register(image_rig, "second")

    await image_rig.containers.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

    assert image_rig.host.calls("POST", "/images/create") == []
    assert await binding_rows(image_rig.db) == []
