"""The group and tag pages: creation, membership, access through a group, and confirmed deletion."""

from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.web.environment_support import (
    DOWN_NAME,
    GROUP_NAME,
    LOCAL_NAME,
    OTHER_TAG_NAME,
    REMOTE_NAME,
    TAG_NAME,
    World,
    listed_names,
    rows,
)
from tests.web.flash_support import flash_texts, land, location
from tests.web.helpers import disclosures_in

OK = 200
SEE_OTHER = 303
UNPROCESSABLE = 422
NOT_FOUND = 404
NEW_GROUP = "staging"


async def test_a_group_is_created_listed_and_a_duplicate_name_in_any_case_is_refused(
    admin_client: AsyncClient, world: World
) -> None:
    created = await admin_client.post(
        "/groups/new", data=csrf_form(admin_client, name=NEW_GROUP, description="pre-production")
    )
    duplicate = await admin_client.post(
        "/groups/new", data=csrf_form(admin_client, name=GROUP_NAME.upper())
    )
    unnamed = await admin_client.post("/groups/new", data=csrf_form(admin_client, name=" "))
    listing = await admin_client.get("/groups")

    assert created.status_code == SEE_OTHER
    assert created.headers["location"].startswith("/groups/")
    assert (duplicate.status_code, unnamed.status_code) == (UNPROCESSABLE, UNPROCESSABLE)
    assert "taken" in duplicate.text
    assert NEW_GROUP in listing.text and GROUP_NAME in listing.text


async def test_access_granted_on_a_group_reaches_its_environments_until_the_group_is_deleted(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    assigned = await admin_client.post(
        f"/groups/{world.group_id}/environments",
        data=csrf_form(admin_client) | {"environment": [str(world.remote_id)]},
    )
    assigned_notices = flash_texts((await land(admin_client, assigned)).text)
    granted = await admin_client.post(
        f"/groups/{world.group_id}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="standard-user"),
    )
    granted_notices = flash_texts((await land(admin_client, granted)).text)
    through_group = await standard_client.get("/")

    await admin_client.post(
        f"/groups/{world.group_id}/delete",
        data=csrf_form(admin_client, expected_name=GROUP_NAME),
    )
    after_delete = await standard_client.get("/")

    assert location(assigned) == f"/groups/{world.group_id}"
    assert location(granted) == f"/groups/{world.group_id}?open=access#access"
    assert assigned_notices == ["Moved 1 environment into the group."]
    assert granted_notices == ["Access granted."]
    assert listed_names(through_group.text) == [LOCAL_NAME, REMOTE_NAME]
    assert listed_names(after_delete.text) == [LOCAL_NAME]
    ungrouped = await rows(world, "SELECT environment_group_id FROM environment")
    assert {row["environment_group_id"] for row in ungrouped} == {None}


async def test_the_groups_access_section_is_closed_on_load_and_open_after_a_grant_or_a_refusal(
    admin_client: AsyncClient, world: World
) -> None:
    path = f"/groups/{world.group_id}"

    closed = await admin_client.get(path)
    requested = await admin_client.get(f"{path}?open=access")
    granted = await admin_client.post(
        f"{path}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="standard-user"),
    )
    landed = await land(admin_client, granted)
    refused = await admin_client.post(
        f"{path}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="nobody"),
    )

    assert disclosures_in(closed.text)["access"] is False
    assert disclosures_in(requested.text)["access"] is True
    assert location(granted) == f"{path}?open=access#access"
    assert disclosures_in(landed.text)["access"] is True
    assert refused.status_code == UNPROCESSABLE
    assert disclosures_in(refused.text)["access"] is True


async def test_the_group_delete_confirmation_lists_what_is_ungrouped_and_a_stale_name_deletes_none(
    admin_client: AsyncClient, world: World
) -> None:
    await admin_client.post(
        f"/groups/{world.group_id}/access",
        data=csrf_form(admin_client, grantee_kind="user", grantee_name="standard-user"),
    )

    confirmation = await admin_client.get(f"/groups/{world.group_id}/delete")
    stale = await admin_client.post(
        f"/groups/{world.group_id}/delete", data=csrf_form(admin_client, expected_name="other")
    )

    assert confirmation.status_code == OK
    assert LOCAL_NAME in confirmation.text and "standard-user" in confirmation.text
    assert "become ungrouped" in confirmation.text
    assert stale.status_code == UNPROCESSABLE
    assert "Changed since it was reviewed" in stale.text
    assert len(await rows(world, "SELECT id FROM environment_group")) == 1


async def test_group_pages_for_a_missing_group_are_not_found(
    admin_client: AsyncClient, world: World
) -> None:
    requests = [("GET", "/groups/9999"), ("GET", "/groups/9999/delete"), ("GET", "/groups/0")]

    statuses = [(await admin_client.request(m, p)).status_code for m, p in requests]

    assert statuses == [NOT_FOUND, NOT_FOUND, UNPROCESSABLE]


async def test_a_tag_is_created_listed_and_a_duplicate_or_a_disallowed_name_is_refused(
    admin_client: AsyncClient, world: World
) -> None:
    created = await admin_client.post("/tags", data=csrf_form(admin_client, name="red"))
    listing = await land(admin_client, created)
    duplicate = await admin_client.post(
        "/tags", data=csrf_form(admin_client, name=TAG_NAME.upper())
    )
    disallowed = await admin_client.post("/tags", data=csrf_form(admin_client, name="a/b"))

    assert location(created) == "/tags"
    assert flash_texts(listing.text) == ["Created the tag red."]
    assert duplicate.status_code == UNPROCESSABLE and "already exists" in duplicate.text
    assert disallowed.status_code == UNPROCESSABLE
    assert TAG_NAME in listing.text and OTHER_TAG_NAME in listing.text


async def test_the_tag_delete_confirmation_names_its_environments_and_a_confirmed_delete_removes_it(
    admin_client: AsyncClient, world: World
) -> None:
    confirmation = await admin_client.get(f"/tags/{world.tag_id}/delete")
    stale = await admin_client.post(
        f"/tags/{world.tag_id}/delete", data=csrf_form(admin_client, expected_name="other")
    )
    deleted = await admin_client.post(
        f"/tags/{world.tag_id}/delete", data=csrf_form(admin_client, expected_name=TAG_NAME)
    )

    assert LOCAL_NAME in confirmation.text and DOWN_NAME in confirmation.text
    assert stale.status_code == UNPROCESSABLE
    assert deleted.status_code == SEE_OTHER
    assert await rows(world, "SELECT id FROM tag WHERE id = ?", (world.tag_id,)) == []
    assert (
        await rows(world, "SELECT id FROM environment_tag WHERE tag_id = ?", (world.tag_id,)) == []
    )


async def test_the_group_and_tag_pages_are_for_administrators_and_a_standard_user_is_forbidden(
    standard_client: AsyncClient, world: World
) -> None:
    requests = [
        ("GET", "/groups"),
        ("GET", "/groups/new"),
        ("POST", "/groups/new"),
        ("GET", f"/groups/{world.group_id}"),
        ("POST", f"/groups/{world.group_id}/delete"),
        ("GET", "/tags"),
        ("POST", "/tags"),
        ("POST", f"/tags/{world.tag_id}/delete"),
    ]

    statuses = {
        (m, p): (await standard_client.request(m, p, data=csrf_form(standard_client))).status_code
        for m, p in requests
    }

    assert set(statuses.values()) == {403}
