"""The registry credential service: storage, edits, duplicates, deletion, grants, audit."""

import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.registry_credential import (
    CredentialFields,
    SecretUnreadableError,
)
from app.registry_client.errors import AddressInvalid, ExtrasInvalid
from app.services.registries import RegistryDetail
from app.services.registry_input import RegistrySpec
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_A_NAME,
    ENV_B,
    ENV_B_NAME,
    MEMBER,
    MEMBER_ID,
    STANDARD,
)
from tests.auth_support import ADMIN_ID, STANDARD_ID
from tests.registry_support import (
    ACCESS_KEY,
    ADDRESS,
    ECR_ADDRESS,
    OTHER_ADDRESS,
    SECRET,
    SECRET_ROTATED,
    USERNAME,
    RegistryRig,
    activity,
    add_anonymous,
    add_ecr,
    add_static,
    bind_image,
    failing_result,
    grant_environment,
    grant_team,
    grant_user,
    issued_token,
    make_team,
    raw_row,
    static_fields,
)

ABSENT_ID = 9999
NOT_A_CERTIFICATE = "this is not a certificate"
CREDENTIAL_COLUMNS = "secret_ciphertext", "derived_secret_ciphertext"


def new_spec(name: str = "registry", **overrides: Any) -> RegistrySpec:
    values: dict[str, Any] = {
        "name": name,
        "provider": "custom",
        "address": ADDRESS,
        "authentication_enabled": True,
        "username": USERNAME,
        "secret": SecretStr(SECRET),
    }
    return RegistrySpec(**{**values, **overrides})


def ecr_spec(name: str = "ecr", **overrides: Any) -> RegistrySpec:
    return new_spec(
        name,
        provider="ecr",
        address=ECR_ADDRESS,
        username=ACCESS_KEY,
        extras={"region": "eu-west-2"},
        **overrides,
    )


async def test_an_anonymous_set_is_created_with_no_user_name_and_no_secret(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(
        ADMIN, new_spec("open", authentication_enabled=False, username="", secret=SecretStr(""))
    )

    row = await raw_row(registry_rig.db, detail.id)
    assert (detail.authentication_enabled, detail.username, detail.secret_stored) == (
        False,
        None,
        False,
    )
    assert (row["username"], row["secret_ciphertext"]) == (None, None)


async def test_a_created_secret_is_stored_as_ciphertext_that_opens_to_the_plaintext(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    row = await raw_row(registry_rig.db, detail.id)
    revealed = await registry_rig.credentials.fetch_revealed(detail.id)
    assert SECRET not in json.dumps(row, default=str)
    assert row["secret_ciphertext"] not in (SECRET, "", None)
    assert revealed is not None
    assert revealed[1].secret is not None
    assert revealed[1].secret.get_secret_value() == SECRET
    assert detail.secret_stored is True


async def test_a_ciphertext_copied_to_another_set_is_refused_there(
    registry_rig: RegistryRig,
) -> None:
    first = await add_static(registry_rig, "first")
    second = await add_static(registry_rig, "second", secret=SECRET_ROTATED)
    await registry_rig.db.execute(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = ?",
        ((await raw_row(registry_rig.db, first.id))["secret_ciphertext"], second.id),
    )

    with pytest.raises(SecretUnreadableError):
        await registry_rig.credentials.fetch_revealed(second.id)


async def test_a_detail_page_model_and_its_repr_hold_no_secret(registry_rig: RegistryRig) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    rendered = repr(detail) + repr(await registry_rig.services.credentials.detail(ADMIN, detail.id))

    assert [s for s in (SECRET, "ciphertext") if s in rendered] == []
    assert "secret_stored=True" in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "extras": {"region": "eu-west-2", "api_token": "x"},
            "provider": "ecr",
            "url": ECR_ADDRESS,
        },
        {"extras": {"unexpected": 1}},
        {"extras": {"region": "eu-west-2"}},
        {"extras": {"nested": {"password": "x"}}},
    ],
    ids=["denylisted-key", "unknown-key", "another-providers-key", "nested-denylisted-key"],
)
async def test_the_repository_refuses_extras_that_are_unknown_or_name_a_secret(
    registry_rig: RegistryRig, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ExtrasInvalid):
        await registry_rig.credentials.create(static_fields(**overrides), SecretStr(SECRET))

    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential") is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"url": "Registry.Example.com"}, AddressInvalid),
        ({"url": "https://registry.example.com"}, AddressInvalid),
        ({"provider": "dockerhub", "url": ADDRESS}, AddressInvalid),
        ({"provider": "quay"}, ValueError),
        ({"provider": "dockerhub", "url": "docker.io", "verify_tls": False}, ValueError),
        ({"provider": "ghcr", "url": "ghcr.io", "ca_bundle": "pem"}, ValueError),
    ],
    ids=[
        "uncanonical-case",
        "scheme-left-on",
        "fixed-address-provider",
        "unknown-provider",
        "no-tls-options-verification-off",
        "no-tls-options-bundle",
    ],
)
async def test_the_repository_refuses_a_row_the_service_would_never_write(
    registry_rig: RegistryRig, overrides: dict[str, Any], error: type[Exception]
) -> None:
    fields = static_fields(**overrides)

    with pytest.raises(error):
        await registry_rig.credentials.create(fields, SecretStr(SECRET))

    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential") is None


async def test_the_repository_refuses_an_ecr_row_whose_address_is_not_an_ecr_address(
    registry_rig: RegistryRig,
) -> None:
    fields = CredentialFields(
        name="x",
        provider="ecr",
        url="registry.example.com",
        authentication_enabled=True,
        username=ACCESS_KEY,
        extras={"region": "eu-west-2"},
    )

    with pytest.raises(AddressInvalid):
        await registry_rig.credentials.create(fields, SecretStr(SECRET))


async def test_an_edit_that_keeps_the_secret_leaves_the_ciphertext_untouched(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    before = (await raw_row(registry_rig.db, detail.id))["secret_ciphertext"]

    await registry_rig.services.credentials.update(
        ADMIN, detail.id, new_spec("renamed", secret=SecretStr(""))
    )

    row = await raw_row(registry_rig.db, detail.id)
    assert (row["name"], row["secret_ciphertext"]) == ("renamed", before)


async def test_an_edit_that_replaces_the_secret_stores_the_new_one(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    await registry_rig.services.credentials.update(
        ADMIN, detail.id, new_spec(secret=SecretStr(SECRET_ROTATED))
    )

    revealed = await registry_rig.credentials.fetch_revealed(detail.id)
    assert revealed is not None
    assert revealed[1].secret is not None
    assert revealed[1].secret.get_secret_value() == SECRET_ROTATED


async def test_an_edit_that_redirects_the_secret_without_entering_it_is_refused_and_changes_nothing(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    before = await raw_row(registry_rig.db, detail.id)

    with pytest.raises(ValidationFailed) as refused:
        await registry_rig.services.credentials.update(
            ADMIN, detail.id, new_spec(address=OTHER_ADDRESS, secret=SecretStr(""))
        )

    assert "secret" in refused.value.fields
    assert await raw_row(registry_rig.db, detail.id) == before


async def test_an_invalid_ca_bundle_is_reported_together_with_the_other_field_errors(
    registry_rig: RegistryRig,
) -> None:
    unusable = new_spec(name=" ", address="", ca_bundle=NOT_A_CERTIFICATE)

    with pytest.raises(ValidationFailed) as refused:
        await registry_rig.services.credentials.create(ADMIN, unusable)

    assert {"name", "address", "ca_bundle"} <= set(refused.value.fields)
    assert NOT_A_CERTIFICATE not in refused.value.fields["ca_bundle"]


async def test_an_invalid_ca_bundle_on_its_own_is_refused_on_that_field_and_creates_no_set(
    registry_rig: RegistryRig,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await registry_rig.services.credentials.create(ADMIN, new_spec(ca_bundle=NOT_A_CERTIFICATE))

    assert set(refused.value.fields) == {"ca_bundle"}
    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential") is None


async def test_every_edit_clears_the_cached_derived_secret_and_stamps_the_change(
    registry_rig: RegistryRig,
) -> None:
    created = await registry_rig.services.credentials.create(ADMIN, ecr_spec())
    stored = await registry_rig.credentials.get(created.id)
    assert stored is not None
    registry_rig.clock.advance(timedelta(minutes=5))
    await registry_rig.credentials.store_derived(
        stored,
        issued_token(),
        expires_at=registry_rig.clock.now + timedelta(hours=1),
        refreshed_at=registry_rig.clock.now,
    )
    registry_rig.clock.advance(timedelta(minutes=1))

    updated = await registry_rig.services.credentials.update(
        ADMIN, created.id, ecr_spec("ecr-renamed", secret=SecretStr(""))
    )

    row = await raw_row(registry_rig.db, created.id)
    assert (
        row["derived_secret_ciphertext"],
        row["derived_secret_expires_at"],
        row["derived_secret_refreshed_at"],
    ) == (None, None, None)
    assert updated.derived is not None
    assert updated.derived.cached is False
    assert updated.updated_at > created.updated_at


async def test_a_name_that_differs_only_in_case_or_unicode_folding_is_refused_on_create_and_edit(
    registry_rig: RegistryRig,
) -> None:
    await registry_rig.services.credentials.create(ADMIN, new_spec("Straße"))
    other = await registry_rig.services.credentials.create(
        ADMIN, new_spec("other", address=OTHER_ADDRESS)
    )

    with pytest.raises(ValidationFailed) as on_create:
        await registry_rig.services.credentials.create(ADMIN, new_spec("STRASSE"))
    with pytest.raises(ValidationFailed) as on_edit:
        await registry_rig.services.credentials.update(
            ADMIN, other.id, new_spec("strasse", address=OTHER_ADDRESS)
        )

    assert "name" in on_create.value.fields
    assert "name" in on_edit.value.fields


async def duplicate_names(rig: RegistryRig, detail: RegistryDetail) -> list[str]:
    return [d.name for d in (await rig.services.credentials.detail(ADMIN, detail.id)).duplicates]


async def test_two_sets_with_the_same_address_and_user_name_both_save_and_each_warns_of_the_other(
    registry_rig: RegistryRig,
) -> None:
    first = await registry_rig.services.credentials.create(ADMIN, new_spec("first"))

    second = await registry_rig.services.credentials.create(ADMIN, new_spec("second"))

    assert [d.name for d in second.duplicates] == ["first"]
    assert await duplicate_names(registry_rig, first) == ["second"]


async def test_user_names_are_compared_ignoring_case_when_detecting_a_duplicate(
    registry_rig: RegistryRig,
) -> None:
    await registry_rig.services.credentials.create(ADMIN, new_spec("first"))

    second = await registry_rig.services.credentials.create(
        ADMIN, new_spec("second", username=USERNAME.upper())
    )

    assert [d.name for d in second.duplicates] == ["first"]


async def test_a_different_user_name_or_address_is_not_a_duplicate(
    registry_rig: RegistryRig,
) -> None:
    await registry_rig.services.credentials.create(ADMIN, new_spec("first"))

    other_user = await registry_rig.services.credentials.create(
        ADMIN, new_spec("second", username="someone-else")
    )
    other_address = await registry_rig.services.credentials.create(
        ADMIN, new_spec("third", address=OTHER_ADDRESS)
    )

    assert (other_user.duplicates, other_address.duplicates) == ((), ())


async def test_two_anonymous_sets_on_one_address_are_duplicates_but_an_authenticated_one_is_not(
    registry_rig: RegistryRig,
) -> None:
    anonymous = {"authentication_enabled": False, "username": "", "secret": SecretStr("")}
    await registry_rig.services.credentials.create(ADMIN, new_spec("open-1", **anonymous))

    second = await registry_rig.services.credentials.create(ADMIN, new_spec("open-2", **anonymous))
    authenticated = await registry_rig.services.credentials.create(ADMIN, new_spec("closed"))

    assert [d.name for d in second.duplicates] == ["open-1"]
    assert authenticated.duplicates == ()


async def test_the_activity_row_of_a_create_counts_its_duplicates(
    registry_rig: RegistryRig,
) -> None:
    await registry_rig.services.credentials.create(ADMIN, new_spec("first"))
    await registry_rig.services.credentials.create(ADMIN, new_spec("second"))

    rows = await activity(registry_rig.db)

    assert [row["detail"]["duplicates"] for row in rows] == [0, 1]


async def test_a_delete_with_the_reviewed_name_removes_the_set_and_its_grants(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await grant_environment(registry_rig.db, detail.id, ENV_A)
    await grant_user(registry_rig.db, detail.id, STANDARD_ID)

    await registry_rig.services.credentials.delete(
        ADMIN, detail.id, expected_name="registry", expected_bindings=0
    )

    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential") is None
    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential_user_access") is None
    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_environment_access") is None


async def test_a_delete_naming_a_different_set_than_the_one_reviewed_deletes_nothing(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await registry_rig.services.credentials.update(
        ADMIN, detail.id, new_spec("renamed", secret=SecretStr(""))
    )

    with pytest.raises(ValidationFailed) as refused:
        await registry_rig.services.credentials.delete(
            ADMIN, detail.id, expected_name="registry", expected_bindings=0
        )

    assert "Changed since it was reviewed" in refused.value.fields["name"]
    assert await raw_row(registry_rig.db, detail.id)


async def test_a_set_with_an_image_bound_to_it_is_deleted_and_the_binding_marked_missing(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await bind_image(registry_rig.db, detail.id, ENV_A)

    await registry_rig.services.credentials.delete(
        ADMIN, detail.id, expected_name="registry", expected_bindings=1
    )

    binding = await registry_rig.db.fetch_one(
        "SELECT state, registry_credential_id, missing_credential_id, missing_credential_name, "
        "missing_reason FROM image_credential_binding"
    )
    assert dict(binding or {}) == {
        "state": "credential_missing",
        "registry_credential_id": None,
        "missing_credential_id": detail.id,
        "missing_credential_name": "registry",
        "missing_reason": "deleted",
    }
    assert await registry_rig.db.fetch_one("SELECT 1 FROM registry_credential") is None


async def test_a_delete_whose_binding_count_changed_since_review_deletes_nothing(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await bind_image(registry_rig.db, detail.id, ENV_A)

    with pytest.raises(ValidationFailed) as refused:
        await registry_rig.services.credentials.delete(
            ADMIN, detail.id, expected_name="registry", expected_bindings=0
        )

    state = await registry_rig.db.fetch_one("SELECT state FROM image_credential_binding")
    assert "Changed since it was reviewed" in refused.value.fields["bindings"]
    assert dict(state or {}) == {"state": "bound"}
    assert await raw_row(registry_rig.db, detail.id)


async def test_the_delete_confirmation_names_the_set_its_grantees_and_what_stays(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    team = await make_team(registry_rig.db, "platform", STANDARD_ID)
    await grant_environment(registry_rig.db, detail.id, ENV_A)
    await grant_user(registry_rig.db, detail.id, STANDARD_ID)
    await grant_team(registry_rig.db, detail.id, team)

    impact = await registry_rig.services.credentials.delete_impact(ADMIN, detail.id)

    assert (impact.name, impact.address, impact.affected_total) == ("registry", ADDRESS, 0)
    assert [e.id for e in impact.environments] == [ENV_A]
    assert [g.name for g in impact.access.users] == [STANDARD.username]
    assert [g.name for g in impact.access.teams] == ["platform"]
    assert "not touched" in impact.pulled_note
    assert "kept" in impact.activity_note


async def test_the_delete_confirmation_names_every_affected_image_by_environment_then_reference(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await bind_image(registry_rig.db, detail.id, ENV_B, f"{ADDRESS}/b:1")
    await bind_image(registry_rig.db, detail.id, ENV_A, f"{ADDRESS}/z:1")
    await bind_image(registry_rig.db, detail.id, ENV_A, f"{ADDRESS}/a:1")

    impact = await registry_rig.services.credentials.delete_impact(ADMIN, detail.id)

    assert [(i.environment_name, i.reference) for i in impact.affected] == [
        (ENV_A_NAME, f"{ADDRESS}/a:1"),
        (ENV_A_NAME, f"{ADDRESS}/z:1"),
        (ENV_B_NAME, f"{ADDRESS}/b:1"),
    ]
    assert (impact.affected_total, impact.affected_hidden, impact.per_environment) == (3, 0, ())
    assert "stay" in impact.bindings_note
    assert "stop" in impact.bindings_note


async def test_a_confirmation_over_500_images_lists_500_and_counts_the_rest_per_environment(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await registry_rig.db.execute(
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 501) "
        "INSERT INTO image_credential_binding (environment_id, image_reference, "
        "registry_credential_id, state, bound_at, updated_at) "
        "SELECT ?, printf('registry.example.com/app:%04d', n), ?, 'bound', 'x', 'x' FROM seq",
        (ENV_A, detail.id),
    )

    impact = await registry_rig.services.credentials.delete_impact(ADMIN, detail.id)

    assert (len(impact.affected), impact.affected_total, impact.affected_hidden) == (500, 501, 1)
    assert [(e.environment_name, e.count) for e in impact.per_environment] == [(ENV_A_NAME, 501)]


async def test_deleting_a_bound_set_marks_each_binding_missing_with_one_related_row_each(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await bind_image(registry_rig.db, detail.id, ENV_A, f"{ADDRESS}/a:1")
    await bind_image(registry_rig.db, detail.id, ENV_B, f"{ADDRESS}/b:1")
    other = await registry_rig.services.credentials.create(ADMIN, new_spec("other"))
    await bind_image(registry_rig.db, other.id, ENV_A, f"{ADDRESS}/untouched:1")

    await registry_rig.services.credentials.delete(
        ADMIN, detail.id, expected_name="registry", expected_bindings=2
    )

    rows = await activity(registry_rig.db)
    related = [r for r in rows if r["action"] == "image_binding_credential_missing"]
    [deleted] = [r for r in rows if r["action"] == "registry_credential_deleted"]
    states = await registry_rig.db.fetch_limited(
        "SELECT image_reference, state, missing_reason FROM image_credential_binding "
        "ORDER BY image_reference",
        (),
        limit=10,
    )
    assert [(r["image_reference"], r["state"], r["missing_reason"]) for r in states] == [
        (f"{ADDRESS}/a:1", "credential_missing", "deleted"),
        (f"{ADDRESS}/b:1", "credential_missing", "deleted"),
        (f"{ADDRESS}/untouched:1", "bound", None),
    ]
    assert sorted((r["target_id"], r["environment_id"]) for r in related) == [
        (f"{ADDRESS}/a:1", ENV_A),
        (f"{ADDRESS}/b:1", ENV_B),
    ]
    assert related[0]["detail"] == {
        "credential_set_id": detail.id,
        "credential_set_name": "registry",
        "missing_reason": "deleted",
    }
    assert deleted["detail"]["bindings_marked_missing"] == 2


async def test_changing_a_bound_sets_address_marks_its_bindings_missing_and_clears_its_default(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    detail = await credentials.create(ADMIN, new_spec())
    await credentials.set_default_for_host(ADMIN, detail.id, enabled=True)
    await bind_image(registry_rig.db, detail.id, ENV_A)

    moved = await credentials.update(
        ADMIN, detail.id, new_spec(address=OTHER_ADDRESS, secret=SecretStr(SECRET))
    )

    rows = await activity(registry_rig.db)
    [related] = [r for r in rows if r["action"] == "image_binding_credential_missing"]
    [updated] = [r for r in rows if r["action"] == "registry_credential_updated"]
    binding = await registry_rig.db.fetch_one(
        "SELECT state, missing_reason, missing_credential_name FROM image_credential_binding"
    )
    assert moved.is_default_for_host is False
    assert dict(binding or {}) == {
        "state": "credential_missing",
        "missing_reason": "address_changed",
        "missing_credential_name": "registry",
    }
    assert related["detail"]["missing_reason"] == "address_changed"
    assert updated["detail"]["bindings_marked_missing"] == 1
    assert updated["detail"]["default_cleared"] is True


async def test_an_edit_that_keeps_the_address_leaves_the_bindings_alone(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    detail = await credentials.create(ADMIN, new_spec())
    await bind_image(registry_rig.db, detail.id, ENV_A)

    await credentials.update(ADMIN, detail.id, new_spec("renamed", secret=SecretStr("")))

    binding = await registry_rig.db.fetch_one("SELECT state FROM image_credential_binding")
    assert dict(binding or {}) == {"state": "bound"}
    assert [r for r in await activity(registry_rig.db) if "binding" in r["action"]] == []


async def test_marking_a_second_set_the_default_for_an_address_clears_the_first(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    first = await credentials.create(ADMIN, new_spec("first"))
    second = await credentials.create(ADMIN, new_spec("second"))
    await credentials.set_default_for_host(ADMIN, first.id, enabled=True)

    await credentials.set_default_for_host(ADMIN, second.id, enabled=True)

    marked = await registry_rig.db.fetch_limited(
        "SELECT id FROM registry_credential WHERE is_default_for_host = 1", (), limit=10
    )
    assert [row["id"] for row in marked] == [second.id]


async def test_the_default_for_an_address_is_independent_of_another_addresss(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    here = await credentials.create(ADMIN, new_spec("here"))
    there = await credentials.create(ADMIN, new_spec("there", address=OTHER_ADDRESS))

    await credentials.set_default_for_host(ADMIN, here.id, enabled=True)
    await credentials.set_default_for_host(ADMIN, there.id, enabled=True)

    marked = await registry_rig.db.fetch_limited(
        "SELECT id FROM registry_credential WHERE is_default_for_host = 1 ORDER BY id",
        (),
        limit=10,
    )
    assert [row["id"] for row in marked] == [here.id, there.id]


async def test_setting_and_clearing_the_default_write_distinct_rows_and_repeating_writes_none(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    detail = await credentials.create(ADMIN, new_spec())

    await credentials.set_default_for_host(ADMIN, detail.id, enabled=True)
    await credentials.set_default_for_host(ADMIN, detail.id, enabled=True)
    await credentials.set_default_for_host(ADMIN, detail.id, enabled=False)
    await credentials.set_default_for_host(ADMIN, detail.id, enabled=False)

    rows = [r for r in await activity(registry_rig.db) if "default" in r["action"]]
    assert [r["action"] for r in rows] == [
        "registry_credential_default_set",
        "registry_credential_default_cleared",
    ]
    assert rows[0]["detail"] == {"name": "registry", "address": ADDRESS}


@pytest.mark.parametrize("actor", [STANDARD, MEMBER], ids=["standard-user", "another-standard"])
async def test_only_an_administrator_may_set_the_default(
    registry_rig: RegistryRig, actor: Principal
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    with pytest.raises(PermissionDenied):
        await registry_rig.services.credentials.set_default_for_host(actor, detail.id, enabled=True)


async def test_setting_the_default_of_an_unknown_set_is_not_found(
    registry_rig: RegistryRig,
) -> None:
    with pytest.raises(ResourceNotFound):
        await registry_rig.services.credentials.set_default_for_host(ADMIN, ABSENT_ID, enabled=True)


async def test_the_offer_for_a_pull_marks_the_default_set_and_carries_no_secret(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    first = await credentials.create(ADMIN, new_spec("first"))
    await credentials.create(ADMIN, new_spec("second"))
    await grant_environment(registry_rig.db, first.id, ENV_A)
    await credentials.set_default_for_host(ADMIN, first.id, enabled=True)

    offered = await credentials.eligible_for_pull(ADMIN, ENV_A)

    assert [(o.name, o.is_default_for_host) for o in offered] == [("first", True)]
    assert SECRET not in repr(offered)


async def test_environment_grants_are_added_listed_and_withdrawn_by_the_service(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    credentials = registry_rig.services.credentials

    await credentials.grant_environment(ADMIN, detail.id, ENV_A)
    granted = await credentials.detail(ADMIN, detail.id)
    await credentials.revoke_environment(ADMIN, detail.id, ENV_A)
    revoked = await credentials.detail(ADMIN, detail.id)

    assert [e.id for e in granted.environments] == [ENV_A]
    assert (granted.offered_nowhere, revoked.offered_nowhere) == (False, True)


@pytest.mark.parametrize(
    "call",
    [
        lambda c, i: c.grant_environment(ADMIN, i, ABSENT_ID),
        lambda c, i: c.revoke_environment(ADMIN, i, ENV_A),
        lambda c, i: c.revoke_environment(ADMIN, i, ABSENT_ID),
        lambda c, i: c.grant(ADMIN, i, user_id=ABSENT_ID),
        lambda c, i: c.revoke(ADMIN, i, user_id=STANDARD_ID),
    ],
    ids=[
        "grant-unknown-environment",
        "revoke-ungranted-environment",
        "revoke-unknown-environment",
        "grant-unknown-user",
        "revoke-ungranted-user",
    ],
)
async def test_a_grant_or_revoke_that_cannot_apply_is_refused_with_a_field_message(
    registry_rig: RegistryRig, call: Callable[[Any, int], Awaitable[None]]
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    with pytest.raises((ValidationFailed, ResourceNotFound)):
        await call(registry_rig.services.credentials, detail.id)


async def test_a_repeated_grant_is_refused_rather_than_stored_twice(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await registry_rig.services.credentials.grant(ADMIN, detail.id, user_id=STANDARD_ID)

    with pytest.raises(ValidationFailed, match="already exists"):
        await registry_rig.services.credentials.grant(ADMIN, detail.id, user_id=STANDARD_ID)


async def test_a_team_grant_is_listed_by_name_and_withdrawn(registry_rig: RegistryRig) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    team = await make_team(registry_rig.db, "platform", STANDARD_ID)
    credentials = registry_rig.services.credentials

    await credentials.grant(ADMIN, detail.id, team_id=team)
    granted = await credentials.detail(ADMIN, detail.id)
    await credentials.revoke(ADMIN, detail.id, team_id=team)
    revoked = await credentials.detail(ADMIN, detail.id)

    assert [g.name for g in granted.access.teams] == ["platform"]
    assert revoked.access.teams == ()


async def offered(rig: RegistryRig, principal: Any, environment_id: int) -> list[str]:
    sets = await rig.services.credentials.eligible_for_pull(principal, environment_id)
    return [s.name for s in sets]


async def seed_offer_matrix(rig: RegistryRig) -> dict[str, int]:
    """Five sets: no user grant, a user grant, a team grant, no environment grant, other host."""
    team = await make_team(rig.db, "platform", STANDARD_ID)
    ids: dict[str, int] = {}
    for name, address in (
        ("ungranted-to-users", ADDRESS),
        ("user-granted", "a.example.com"),
        ("team-granted", "b.example.com"),
        ("no-environment", "c.example.com"),
        ("other-environment", "d.example.com"),
    ):
        ids[name] = (await add_anonymous(rig, name, url=address)).id
    for name in ("ungranted-to-users", "user-granted", "team-granted"):
        await grant_environment(rig.db, ids[name], ENV_A)
    await grant_environment(rig.db, ids["other-environment"], ENV_B)
    await grant_user(rig.db, ids["user-granted"], STANDARD_ID)
    await grant_user(rig.db, ids["no-environment"], STANDARD_ID)
    await grant_user(rig.db, ids["other-environment"], STANDARD_ID)
    await grant_team(rig.db, ids["team-granted"], team)
    return ids


async def test_an_administrator_is_offered_every_set_granted_to_the_environment_and_no_other(
    registry_rig: RegistryRig,
) -> None:
    await seed_offer_matrix(registry_rig)

    names = await offered(registry_rig, ADMIN, ENV_A)

    assert names == ["team-granted", "ungranted-to-users", "user-granted"]


async def test_a_standard_user_is_offered_only_sets_granted_to_both_the_environment_and_them(
    registry_rig: RegistryRig,
) -> None:
    await seed_offer_matrix(registry_rig)

    on_a = await offered(registry_rig, STANDARD, ENV_A)
    on_b = await offered(registry_rig, STANDARD, ENV_B)

    assert on_a == ["team-granted", "user-granted"]
    assert on_b == ["other-environment"]


async def test_a_standard_user_with_no_grant_on_any_set_is_offered_none(
    registry_rig: RegistryRig,
) -> None:
    await seed_offer_matrix(registry_rig)

    assert await offered(registry_rig, MEMBER, ENV_A) == []


async def test_withdrawing_a_user_grant_or_an_environment_grant_withdraws_the_offer_at_once(
    registry_rig: RegistryRig,
) -> None:
    ids = await seed_offer_matrix(registry_rig)
    credentials = registry_rig.services.credentials

    await credentials.revoke(ADMIN, ids["user-granted"], user_id=STANDARD_ID)
    after_user = await offered(registry_rig, STANDARD, ENV_A)
    await credentials.revoke_environment(ADMIN, ids["team-granted"], ENV_A)
    after_environment = await offered(registry_rig, STANDARD, ENV_A)

    assert after_user == ["team-granted"]
    assert after_environment == []


async def test_a_standard_user_who_cannot_see_the_environment_gets_not_found(
    registry_rig: RegistryRig,
) -> None:
    await seed_offer_matrix(registry_rig)
    await EnvironmentAccessRepository(registry_rig.db).remove_user(
        "environment", ENV_A, STANDARD_ID
    )

    with pytest.raises(ResourceNotFound):
        await registry_rig.services.credentials.eligible_for_pull(STANDARD, ENV_A)


async def test_the_test_action_uses_the_saved_settings_and_stored_secret_only(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    report = await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    (call,) = registry_rig.probes.calls
    assert (call.target.address, call.target.provider, call.target.verify_tls) == (
        ADDRESS,
        "custom",
        True,
    )
    assert call.login is not None
    assert (call.login.username, call.login.secret.get_secret_value()) == (USERNAME, SECRET)
    assert call.key is not None
    assert call.key[0] == detail.id
    assert report.passed is True


async def test_the_test_of_an_anonymous_set_sends_no_login(registry_rig: RegistryRig) -> None:
    detail = await registry_rig.services.credentials.create(
        ADMIN, new_spec("open", authentication_enabled=False, username="", secret=SecretStr(""))
    )

    await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    assert registry_rig.probes.calls[0].login is None


async def test_a_failed_test_is_a_report_naming_the_stage_and_never_an_error(
    registry_rig: RegistryRig,
) -> None:
    registry_rig.probes.result = failing_result()
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())

    report = await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    assert (report.passed, report.failed_stage) == (False, "tcp")
    assert [stage.stage for stage in report.stages] == ["tcp"]
    assert SECRET not in repr(report)


async def test_the_connection_key_changes_when_the_secret_is_replaced(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await registry_rig.services.credentials.test_connection(ADMIN, detail.id)
    await registry_rig.services.credentials.update(
        ADMIN, detail.id, new_spec(secret=SecretStr(SECRET_ROTATED))
    )

    await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    first, second = registry_rig.probes.calls
    assert first.key is not None
    assert second.key is not None
    assert first.key[1] != second.key[1]


async def test_a_test_of_a_set_whose_secret_cannot_be_read_says_to_enter_it_again(
    registry_rig: RegistryRig,
) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, new_spec())
    await registry_rig.db.execute(
        "UPDATE registry_credential SET secret_ciphertext = 'v1:garbage' WHERE id = ?", (detail.id,)
    )

    with pytest.raises(ValidationFailed, match="Enter it again"):
        await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    assert registry_rig.probes.calls == []


async def test_an_ecr_test_does_not_write_a_derived_secret(registry_rig: RegistryRig) -> None:
    detail = await registry_rig.services.credentials.create(ADMIN, ecr_spec())

    await registry_rig.services.credentials.test_connection(ADMIN, detail.id)

    row = await raw_row(registry_rig.db, detail.id)
    assert (row["derived_secret_ciphertext"], row["derived_secret_expires_at"]) == (None, None)
    assert registry_rig.deriver.requests == []


async def test_each_mutation_writes_one_success_row_and_no_row_holds_a_secret(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    created = await credentials.create(ADMIN, new_spec())
    await credentials.update(
        ADMIN,
        created.id,
        new_spec("renamed", secret=SecretStr(SECRET_ROTATED), username="rotated-user"),
    )
    await credentials.test_connection(ADMIN, created.id)
    await credentials.grant_environment(ADMIN, created.id, ENV_A)
    await credentials.revoke_environment(ADMIN, created.id, ENV_A)
    await credentials.grant(ADMIN, created.id, user_id=STANDARD_ID)
    await credentials.revoke(ADMIN, created.id, user_id=STANDARD_ID)
    await credentials.delete(ADMIN, created.id, expected_name="renamed", expected_bindings=0)

    rows = await activity(registry_rig.db)

    assert [row["action"] for row in rows] == [
        "registry_credential_created",
        "registry_credential_updated",
        "registry_credential_tested",
        "registry_credential_environment_granted",
        "registry_credential_environment_revoked",
        "registry_credential_access_granted",
        "registry_credential_access_revoked",
        "registry_credential_deleted",
    ]
    assert {(row["result"], row["actor_user_id"], row["target_type"]) for row in rows} == {
        ("success", ADMIN_ID, "registry_credential")
    }
    assert {row["target_id"] for row in rows} == {str(created.id)}
    blob = json.dumps(rows)
    assert [s for s in (SECRET, SECRET_ROTATED, USERNAME, "rotated-user") if s in blob] == []


async def test_the_rows_record_the_environment_the_address_and_the_names_of_what_changed(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    created = await credentials.create(ADMIN, new_spec())
    await credentials.update(
        ADMIN,
        created.id,
        new_spec(
            "renamed", address=OTHER_ADDRESS, username="rotated-user", secret=SecretStr(SECRET)
        ),
    )
    await credentials.test_connection(ADMIN, created.id)
    await credentials.grant_environment(ADMIN, created.id, ENV_B)
    await credentials.grant(ADMIN, created.id, user_id=STANDARD_ID)

    rows = {row["action"]: row for row in await activity(registry_rig.db)}

    assert rows["registry_credential_created"]["detail"] == {
        "name": "registry",
        "provider": "custom",
        "address": ADDRESS,
        "authentication_enabled": True,
        "verify_tls": True,
        "ca_bundle": False,
        "duplicates": 0,
    }
    updated = rows["registry_credential_updated"]["detail"]
    assert updated["changed"].split(", ") == ["name", "address", "username"]
    assert (updated["address"], updated["secret_replaced"]) == (OTHER_ADDRESS, True)
    assert rows["registry_credential_tested"]["detail"]["address"] == OTHER_ADDRESS
    granted = rows["registry_credential_environment_granted"]
    assert (granted["environment_id"], granted["detail"]["environment_name"]) == (
        ENV_B,
        ENV_B_NAME,
    )
    assert rows["registry_credential_access_granted"]["detail"] == {
        "grantee_kind": "user",
        "grantee_name": STANDARD.username,
    }


async def test_an_edit_that_changes_nothing_writes_no_row(registry_rig: RegistryRig) -> None:
    credentials = registry_rig.services.credentials
    created = await credentials.create(ADMIN, new_spec())

    await credentials.update(ADMIN, created.id, new_spec(secret=SecretStr("")))

    assert [row["action"] for row in await activity(registry_rig.db)] == [
        "registry_credential_created"
    ]


async def test_a_refused_edit_leaves_a_failure_row_that_quotes_nothing(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    created = await credentials.create(ADMIN, new_spec())

    with pytest.raises(ValidationFailed):
        await credentials.update(
            ADMIN, created.id, new_spec(address=OTHER_ADDRESS, secret=SecretStr(""))
        )

    failure = (await activity(registry_rig.db))[-1]
    assert (failure["action"], failure["result"]) == ("registry_credential_updated", "failure")
    assert [s for s in (SECRET, USERNAME) if s in json.dumps(failure)] == []


ANY_SERVICE_CALL = [
    pytest.param(lambda c: c.page(STANDARD), id="page"),
    pytest.param(lambda c: c.detail(STANDARD, ABSENT_ID), id="detail"),
    pytest.param(lambda c: c.form_context(STANDARD, credential_id=ABSENT_ID), id="form-context"),
    pytest.param(lambda c: c.delete_impact(STANDARD, ABSENT_ID), id="delete-impact"),
    pytest.param(lambda c: c.resolve_grantee(STANDARD, username="x"), id="resolve-grantee"),
    pytest.param(lambda c: c.resolve_environment(STANDARD, "x"), id="resolve-environment"),
    pytest.param(lambda c: c.create(STANDARD, new_spec()), id="create"),
    pytest.param(lambda c: c.update(STANDARD, ABSENT_ID, new_spec()), id="update"),
    pytest.param(
        lambda c: c.delete(STANDARD, ABSENT_ID, expected_name="x", expected_bindings=0), id="delete"
    ),
    pytest.param(lambda c: c.test_connection(STANDARD, ABSENT_ID), id="test"),
    pytest.param(lambda c: c.grant_environment(STANDARD, ABSENT_ID, ENV_A), id="grant-environment"),
    pytest.param(
        lambda c: c.revoke_environment(STANDARD, ABSENT_ID, ENV_A), id="revoke-environment"
    ),
    pytest.param(lambda c: c.grant(STANDARD, ABSENT_ID, user_id=MEMBER_ID), id="grant"),
    pytest.param(lambda c: c.revoke(STANDARD, ABSENT_ID, user_id=MEMBER_ID), id="revoke"),
]


@pytest.mark.parametrize("call", ANY_SERVICE_CALL)
async def test_a_standard_user_is_refused_before_any_lookup_or_row(
    registry_rig: RegistryRig, call: Callable[[Any], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionDenied):
        await call(registry_rig.services.credentials)

    assert await activity(registry_rig.db) == []
    assert registry_rig.probes.calls == []


async def test_the_list_pages_by_name_and_counts_grants_without_a_username_or_secret(
    registry_rig: RegistryRig,
) -> None:
    first = await add_static(registry_rig, "b-set")
    await add_anonymous(registry_rig, "a-set")
    await grant_environment(registry_rig.db, first.id, ENV_A)
    await grant_user(registry_rig.db, first.id, STANDARD_ID)

    page = await registry_rig.services.credentials.page(ADMIN, limit=1)
    rest = await registry_rig.services.credentials.page(ADMIN, cursor=page.next_cursor)

    assert [row.name for row in page.items] == ["a-set"]
    assert [(row.name, row.environment_count, row.grantee_count) for row in rest.items] == [
        ("b-set", 1, 1)
    ]
    assert [s for s in (SECRET, USERNAME) if s in repr(page) + repr(rest)] == []


async def test_a_form_for_a_changed_provider_drops_the_stored_secret_flag_and_resets_defaults(
    registry_rig: RegistryRig,
) -> None:
    created = await registry_rig.services.credentials.create(ADMIN, new_spec())
    credentials = registry_rig.services.credentials

    same = await credentials.form_context(ADMIN, credential_id=created.id)
    switched = await credentials.form_context(ADMIN, credential_id=created.id, provider="gitlab")

    assert (same.secret_stored, same.address, same.username) == (True, ADDRESS, USERNAME)
    assert (switched.secret_stored, switched.address) == (False, "registry.gitlab.com")
    assert switched.provider == "gitlab"


async def test_an_ecr_detail_reports_the_cached_token_state_by_stamp_only(
    registry_rig: RegistryRig,
) -> None:
    ecr = await add_ecr(registry_rig)

    detail = await registry_rig.services.credentials.detail(ADMIN, ecr.id)

    assert detail.derived is not None
    assert (detail.derived.cached, detail.derived.expired) == (False, False)
    assert dict(detail.extras) == {"region": "eu-west-2"}


async def test_no_view_model_the_service_hands_out_holds_a_secret_in_its_repr(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    static = await credentials.create(ADMIN, new_spec())
    ecr = await credentials.create(ADMIN, ecr_spec())
    await grant_environment(registry_rig.db, static.id, ENV_A)
    sealed = (await raw_row(registry_rig.db, static.id))["secret_ciphertext"]

    shown = [
        await credentials.detail(ADMIN, static.id),
        await credentials.detail(ADMIN, ecr.id),
        await credentials.form_context(ADMIN, credential_id=static.id),
        await credentials.form_state(ADMIN, new_spec(), credential_id=static.id),
        await credentials.delete_impact(ADMIN, static.id),
        await credentials.page(ADMIN),
        await credentials.test_connection(ADMIN, static.id),
        await credentials.eligible_for_pull(ADMIN, ENV_A),
        await registry_rig.credentials.get(static.id),
        await registry_rig.credentials.fetch_revealed(static.id),
    ]

    text = " ".join(repr(item) for item in shown)
    assert [marker for marker in (SECRET, sealed) if marker in text] == []


async def test_the_list_the_offers_and_the_delete_impact_carry_no_user_name(
    registry_rig: RegistryRig,
) -> None:
    credentials = registry_rig.services.credentials
    static = await credentials.create(ADMIN, new_spec())
    await grant_environment(registry_rig.db, static.id, ENV_A)

    shown = [
        await credentials.page(ADMIN),
        await credentials.eligible_for_pull(ADMIN, ENV_A),
        await credentials.delete_impact(ADMIN, static.id),
        await registry_rig.credentials.get(static.id),
    ]

    assert USERNAME not in " ".join(repr(item) for item in shown)
