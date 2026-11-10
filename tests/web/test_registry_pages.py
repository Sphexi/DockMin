"""The registry pages: forms, creation, detail, grants, the connection test, deletion and access."""

import re

from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.web import registry_routes
from tests.auth_support import STANDARD_USERNAME, csrf_form, csrf_headers
from tests.registry_support import (
    BOUND_REFERENCE,
    ScriptedRegistryProbes,
    bind_image,
    failing_result,
    make_team,
)
from tests.support import present
from tests.web.environment_support import LOCAL_NAME, REMOTE_NAME, World
from tests.web.flash_support import flash_texts, flashes_in, land, location
from tests.web.helpers import field_error, form_values, value_of
from tests.web.registry_support import (
    ADDRESS,
    FORBIDDEN,
    HTMX,
    NOT_FOUND,
    OK,
    OTHER_ADDRESS,
    SECRET,
    SEE_OTHER,
    UNPROCESSABLE,
    USERNAME,
    activity_rows,
    anonymous_form,
    control,
    create_set,
    custom_form,
    input_names,
    provider_forms,
    registry_form,
    script_probes,
    visible_text,
)

REGISTRY_ENDPOINTS = 15
RESULT_PATH = "/results/"
UNREADABLE_BUNDLE = "-----BEGIN CERTIFICATE-----\nQUFBQUJCQkI=\n-----END CERTIFICATE-----"
ABSENT_ID = 9999
NO_STORE = "no-store"
DEFAULT_BADGE = re.compile(r'data-tone="ok">Default</span>')
EXPECTED_FORMS = {
    "dockerhub": (frozenset(), False, False, True),
    "ghcr": (frozenset({"organisation", "use_organisation"}), False, False, True),
    "acr": (frozenset(), True, False, True),
    "gitlab": (frozenset({"instance_url", "project_id", "project_path"}), True, True, True),
    "ecr": (frozenset({"region"}), True, False, False),
    "custom": (frozenset(), True, True, True),
}


def registry_requests(credential_id: int) -> list[tuple[str, str]]:
    """Return every (method, path) the registry router serves, with ids filled in."""
    return [
        (method, route.path.replace("{credential_id}", str(credential_id)))
        for route in registry_routes.router.routes
        if isinstance(route, APIRoute)
        for method in sorted(present(route.methods) - {"HEAD", "OPTIONS"})
    ]


async def send_everywhere(
    client: AsyncClient, credential_ids: tuple[int, ...]
) -> dict[tuple[str, str], int]:
    """Send every registry route once per id with a valid token and a plausible form."""
    results: dict[tuple[str, str], int] = {}
    for credential_id in credential_ids:
        for method, path in registry_requests(credential_id):
            response = await client.request(
                method, path, data=csrf_form(client, name="x", provider="custom")
            )
            results[(method, path)] = response.status_code
    return results


def test_every_registry_route_is_found_for_the_access_and_forgery_tests() -> None:
    assert len(registry_requests(1)) == REGISTRY_ENDPOINTS


async def test_each_providers_form_renders_its_own_extras_and_no_others(
    admin_client: AsyncClient,
) -> None:
    forms = await provider_forms(admin_client)
    hub = (await admin_client.get("/registries/new", params={"provider": "dockerhub"})).text
    ghcr = (await admin_client.get("/registries/new", params={"provider": "ghcr"})).text

    assert forms == EXPECTED_FORMS
    assert ("docker.io" in hub, "ghcr.io" in ghcr) == (True, True)
    assert "address" not in input_names(hub) | input_names(ghcr)


async def test_the_provider_selector_swaps_only_the_fields_with_script_and_reloads_without_it(
    admin_client: AsyncClient,
) -> None:
    page = (await admin_client.get("/registries/new")).text
    swap = await admin_client.post(
        "/registries/form-fields",
        headers=HTMX,
        data=registry_form(
            admin_client, name="keep-me", provider="ghcr", username=USERNAME, secret=SECRET
        ),
    )
    whole = await admin_client.post(
        "/registries/form-fields",
        data=registry_form(admin_client, name="typed", provider="ecr", username=USERNAME),
    )
    fallback = await admin_client.get("/registries/new", params={"provider": "nonsense"})

    selector = control(page, "provider")
    assert selector is not None
    assert selector["hx-post"] == "/registries/form-fields"
    assert "not secret" in selector["hx-params"]
    assert "ca_bundle" in selector["hx-params"]
    assert "<html" not in swap.text
    assert input_names(swap.text) >= {"provider", "organisation", "use_organisation", "username"}
    assert value_of(swap.text, "username") == USERNAME
    assert SECRET not in swap.text
    secret_field = control(swap.text, "secret")
    assert secret_field is not None
    assert "value" not in secret_field
    assert "<html" in whole.text
    assert value_of(whole.text, "name") == "typed"
    assert "region" in input_names(whole.text)
    assert fallback.status_code == OK
    assert "docker.io" in fallback.text


async def test_creating_anonymous_and_authenticated_sets_then_editing_them(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    anonymous = await admin_client.post("/registries", data=anonymous_form(admin_client))
    credential_id = await create_set(admin_client, custom_form(admin_client))

    open_page = (await admin_client.get(anonymous.headers["location"])).text
    detail = (await admin_client.get(f"/registries/{credential_id}")).text
    edit = (await admin_client.get(f"/registries/{credential_id}/edit")).text
    switched = (
        await admin_client.get(f"/registries/{credential_id}/edit", params={"provider": "gitlab"})
    ).text
    kept = await admin_client.post(
        f"/registries/{credential_id}/edit",
        data=custom_form(admin_client, name="renamed", secret="", credential_id=str(credential_id)),
    )
    saved_page = await land(admin_client, kept)
    redirected = await admin_client.post(
        f"/registries/{credential_id}/edit",
        data=custom_form(admin_client, address=OTHER_ADDRESS, secret=""),
    )

    assert anonymous.status_code == SEE_OTHER
    assert "Anonymous" in open_page
    assert "Stored. It is never shown." not in open_page
    assert USERNAME not in open_page
    assert "Stored. It is never shown." in detail
    assert USERNAME in detail
    assert SECRET not in detail + edit
    secret_field = control(edit, "secret")
    assert secret_field is not None
    assert "value" not in secret_field
    assert value_of(edit, "username") == USERNAME
    assert "leave blank to keep it" in edit
    assert "instance_url" in input_names(switched)
    assert value_of(switched, "address") == "registry.gitlab.com"
    assert "leave blank to keep it" not in switched
    assert location(kept) == f"/registries/{credential_id}"
    assert flash_texts(saved_page.text) == ["Changes saved."]
    assert "renamed" in saved_page.text
    assert redirected.status_code == UNPROCESSABLE
    assert "Enter the secret again" in (field_error(redirected.text, "field-secret") or "")
    assert value_of(redirected.text, "address") == OTHER_ADDRESS


async def test_refused_creates_keep_what_was_typed_except_secrets_and_certificates(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    typed = custom_form(
        admin_client,
        name="",
        username="bad:name",
        address="http://registry.example.com",
        secret=SECRET,
    )
    bundle = "-----BEGIN CERTIFICATE-----\nQUFBQUJCQkI=\n-----END CERTIFICATE-----"

    refused = await admin_client.post("/registries", data=typed)
    bad_bundle = await admin_client.post(
        "/registries", data=custom_form(admin_client, ca_bundle=bundle)
    )
    oversized = await admin_client.post(
        "/registries", data=custom_form(admin_client, name="n" * 600)
    )
    listing = (await admin_client.get("/registries")).text

    assert refused.status_code == UNPROCESSABLE
    assert value_of(refused.text, "username") == "bad:name"
    assert value_of(refused.text, "address") == "http://registry.example.com"
    assert SECRET not in refused.text
    assert field_error(refused.text, "field-name") is not None
    assert field_error(refused.text, "field-username") is not None
    assert "Secrets and certificates are never sent back" in refused.text
    assert bad_bundle.status_code == UNPROCESSABLE
    assert "could not be read" in (field_error(bad_bundle.text, "field-ca_bundle") or "")
    assert "QUFBQUJCQkI" not in bad_bundle.text
    assert oversized.status_code == UNPROCESSABLE
    assert "bad:name" not in listing
    assert [row["result"] for row in await activity_rows(started_app)] == ["failure", "failure"]


async def test_an_unreadable_ca_bundle_is_reported_together_with_the_other_field_errors(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/registries", data=custom_form(admin_client, name="", ca_bundle=UNREADABLE_BUNDLE)
    )

    assert response.status_code == UNPROCESSABLE
    assert field_error(response.text, "field-name") is not None
    assert "could not be read" in (field_error(response.text, "field-ca_bundle") or "")


async def test_duplicates_warn_on_both_pages_and_the_list_pages_by_name(
    admin_client: AsyncClient,
) -> None:
    first = await create_set(admin_client, custom_form(admin_client, "b-first"))
    second = await create_set(admin_client, custom_form(admin_client, "a-second"))

    first_page = (await admin_client.get(f"/registries/{first}")).text
    second_page = (await admin_client.get(f"/registries/{second}")).text
    listing = await admin_client.get("/registries")
    fragment = await admin_client.get("/registries", headers=HTMX)
    bad_cursor = await admin_client.get("/registries", params={"cursor": "not-a-cursor"})
    bad_limit = await admin_client.get("/registries", params={"limit": "0"})

    for page, other in ((first_page, second), (second_page, first)):
        assert "Another set uses the same address and username" in page
        assert f'href="/registries/{other}"' in page
    assert re.findall(r'href="/registries/\d+">([^<]+)</a>', listing.text) == [
        "a-second",
        "b-first",
    ]
    assert "<html" not in fragment.text
    assert "a-second" in fragment.text
    assert USERNAME not in listing.text + fragment.text
    assert (bad_cursor.status_code, bad_limit.status_code) == (400, 422)


async def test_pages_are_never_cached_ignore_a_notice_query_and_answer_not_found_for_unknown_sets(
    admin_client: AsyncClient,
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client))
    paths = [
        "/registries",
        "/registries/new",
        f"/registries/{credential_id}",
        f"/registries/{credential_id}/edit",
        f"/registries/{credential_id}/delete",
    ]
    absent = [f"/registries/{ABSENT_ID}", f"/registries/{ABSENT_ID}/edit"]
    absent.append(f"/registries/{ABSENT_ID}/delete")

    cache_control = [(await admin_client.get(path)).headers.get("cache-control") for path in paths]
    notice = await admin_client.get(
        f"/registries/{credential_id}", params={"notice": "<script>alert(1)</script>"}
    )
    statuses = [(await admin_client.get(path)).status_code for path in absent]

    assert cache_control == [NO_STORE] * len(paths)
    assert "<script>alert" not in notice.text
    assert flashes_in(notice.text) == []
    assert statuses == [NOT_FOUND] * 3


async def test_an_environment_is_granted_by_name_listed_and_removed(
    admin_client: AsyncClient, world: World
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client))
    bare = (await admin_client.get(f"/registries/{credential_id}")).text

    granted = await admin_client.post(
        f"/registries/{credential_id}/environments",
        data=registry_form(admin_client, environment_name=LOCAL_NAME),
    )
    page = await land(admin_client, granted)
    unknown = await admin_client.post(
        f"/registries/{credential_id}/environments",
        data=registry_form(admin_client, environment_name="no-such-environment"),
    )
    removed = await admin_client.post(
        f"/registries/{credential_id}/environments/remove",
        params={"environment_id": world.local_id},
        data=registry_form(admin_client),
    )
    after = await land(admin_client, removed)

    assert "This set is offered nowhere." in visible_text(bare)
    assert location(granted) == f"/registries/{credential_id}"
    assert LOCAL_NAME in page.text
    assert flash_texts(page.text) == ["Environment granted."]
    assert unknown.status_code == UNPROCESSABLE
    assert "No environment has that name." in unknown.text
    assert "no-such-environment" in unknown.text
    assert location(removed) == f"/registries/{credential_id}"
    assert flash_texts(after.text) == ["Environment removed."]
    assert "This set is offered nowhere." in visible_text(after.text)


async def test_a_user_and_a_team_are_granted_by_name_withdrawn_and_bad_requests_are_refused(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client))
    async with started_app.state.database.session() as db:
        team_id = await make_team(db, "platform")
    path = f"/registries/{credential_id}/access"

    user_grant = await admin_client.post(
        path,
        data=registry_form(admin_client, grantee_kind="user", grantee_name=STANDARD_USERNAME),
    )
    team_grant = await admin_client.post(
        path, data=registry_form(admin_client, grantee_kind="team", grantee_name="platform")
    )
    page = (await land(admin_client, team_grant)).text
    removed = await admin_client.post(
        f"{path}/remove", params={"team_id": team_id}, data=registry_form(admin_client)
    )
    after = await land(admin_client, removed)
    no_user = await admin_client.post(
        path, data=registry_form(admin_client, grantee_kind="user", grantee_name="")
    )
    no_team = await admin_client.post(
        path, data=registry_form(admin_client, grantee_kind="team", grantee_name="")
    )
    no_kind = await admin_client.post(
        path, data=registry_form(admin_client, grantee_kind="robot", grantee_name="x")
    )

    assert (user_grant.status_code, team_grant.status_code) == (SEE_OTHER, SEE_OTHER)
    assert STANDARD_USERNAME in page
    assert "platform" in page
    assert location(removed) == f"/registries/{credential_id}"
    assert flash_texts(after.text) == ["Access removed."]
    assert (no_user.status_code, no_team.status_code, no_kind.status_code) == (UNPROCESSABLE,) * 3
    assert "Enter a username." in no_user.text
    assert "Enter a team name." in no_team.text
    assert "Choose whether" in no_kind.text


async def test_the_connection_test_swaps_in_a_staged_report_and_a_browser_gets_the_page(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    script_probes(started_app, ScriptedRegistryProbes(result=failing_result()))
    credential_id = await create_set(admin_client, custom_form(admin_client))
    untested = (await admin_client.get(f"/registries/{credential_id}")).text
    path = f"/registries/{credential_id}/test"

    fragment = await admin_client.post(path, headers=HTMX, data=registry_form(admin_client))
    page = await admin_client.post(path, data=registry_form(admin_client))
    async with started_app.state.database.session() as db:
        await db.execute(
            "UPDATE registry_credential SET secret_ciphertext = 'v1:garbage' WHERE id = ?",
            (credential_id,),
        )
    unreadable = await admin_client.post(path, headers=HTMX, data=registry_form(admin_client))

    assert "Not tested since saved." in untested
    assert fragment.status_code == page.status_code == OK
    assert "<html" not in fragment.text
    assert 'id="registry-test"' in fragment.text
    assert "Failed" in fragment.text
    assert "The connection was refused." in fragment.text
    assert "TCP connection" in fragment.text
    assert "Run from this server, not from a Docker host." in fragment.text
    assert "<html" in page.text
    assert SECRET not in fragment.text + page.text
    assert unreadable.status_code == UNPROCESSABLE
    assert "Enter it again" in unreadable.text


async def test_the_delete_confirmation_names_what_is_lost_and_only_the_reviewed_name_deletes(
    admin_client: AsyncClient, world: World
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, "doomed"))
    await admin_client.post(
        f"/registries/{credential_id}/environments",
        data=registry_form(admin_client, environment_name=REMOTE_NAME),
    )
    await admin_client.post(
        f"/registries/{credential_id}/access",
        data=registry_form(admin_client, grantee_kind="user", grantee_name=STANDARD_USERNAME),
    )
    path = f"/registries/{credential_id}/delete"

    confirmation = (await admin_client.get(path)).text
    wrong = await admin_client.post(
        path,
        data=registry_form(admin_client, expected_name="something-else", expected_bindings="0"),
    )
    still_there = await admin_client.get(f"/registries/{credential_id}")
    right = await admin_client.post(
        path, data=registry_form(admin_client, expected_name="doomed", expected_bindings="0")
    )
    result = await land(admin_client, right)
    gone = await admin_client.get(f"/registries/{credential_id}")

    for text in ("doomed", ADDRESS, REMOTE_NAME, STANDARD_USERNAME, "not touched", "kept"):
        assert text in confirmation
    assert control(confirmation, "expected_name") is not None
    assert SECRET not in confirmation
    assert wrong.status_code == UNPROCESSABLE
    assert still_there.status_code == OK
    assert (right.status_code, location(right).startswith(RESULT_PATH)) == (SEE_OTHER, True)
    assert "doomed" in result.text
    assert gone.status_code == NOT_FOUND


async def test_the_delete_confirmation_lists_the_bound_images_and_deleting_marks_them_missing(
    admin_client: AsyncClient, started_app: FastAPI, world: World
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, "bound"))
    async with started_app.state.database.session() as db:
        await bind_image(db, credential_id, world.local_id)
    path = f"/registries/{credential_id}/delete"

    confirmation = visible_text((await admin_client.get(path)).text)
    stale = await admin_client.post(
        path, data=registry_form(admin_client, expected_name="bound", expected_bindings="0")
    )
    deleted = await admin_client.post(
        path, data=registry_form(admin_client, expected_name="bound", expected_bindings="1")
    )
    result = await land(admin_client, deleted)

    async with started_app.state.database.session() as db:
        state = await db.fetch_one("SELECT state FROM image_credential_binding")
    assert LOCAL_NAME in confirmation
    assert BOUND_REFERENCE in confirmation
    assert "1 image is affected" in confirmation
    assert stale.status_code == UNPROCESSABLE
    assert "Changed since it was reviewed" in stale.text
    assert (deleted.status_code, location(deleted).startswith(RESULT_PATH)) == (SEE_OTHER, True)
    assert "Bound set deleted" in visible_text(result.text)
    assert dict(state or {}) == {"state": "credential_missing"}


async def test_the_default_for_host_toggle_marks_a_set_and_clearing_it_removes_the_mark(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, "preferred"))
    path = f"/registries/{credential_id}/default"

    marked = await admin_client.post(path, data=registry_form(admin_client, enabled="yes"))
    marked_list = (await admin_client.get("/registries")).text
    marked_page = (await admin_client.get(f"/registries/{credential_id}")).text
    cleared = await admin_client.post(path, data=registry_form(admin_client, enabled="no"))
    cleared_page = (await admin_client.get(f"/registries/{credential_id}")).text
    cleared_list = (await admin_client.get("/registries")).text

    assert (marked.status_code, cleared.status_code) == (SEE_OTHER, SEE_OTHER)
    assert DEFAULT_BADGE.search(marked_list) is not None
    assert DEFAULT_BADGE.search(cleared_list) is None
    assert "Clear the default" in marked_page
    assert "Make the default" in cleared_page
    actions = [row["action"] for row in await activity_rows(started_app)]
    assert actions[-2:] == [
        "registry_credential_default_set",
        "registry_credential_default_cleared",
    ]


async def test_a_standard_user_is_forbidden_every_registry_route_even_for_a_set_that_does_not_exist(
    standard_client: AsyncClient,
) -> None:
    results = await send_everywhere(standard_client, (1, ABSENT_ID))

    expected = set(registry_requests(1)) | set(registry_requests(ABSENT_ID))
    assert set(results) == expected
    assert set(results.values()) == {FORBIDDEN}


async def test_an_anonymous_visitor_is_sent_to_sign_in_for_every_registry_route(
    anonymous_client: AsyncClient,
) -> None:
    statuses = {
        (method, path): (
            await anonymous_client.request(
                method, path, data=csrf_form(anonymous_client), follow_redirects=False
            )
        ).status_code
        for method, path in registry_requests(1)
    }

    assert set(statuses.values()) == {SEE_OTHER}
    assert len(statuses) == REGISTRY_ENDPOINTS


async def test_a_registry_post_without_the_forgery_token_or_from_a_foreign_origin_changes_nothing(
    admin_client: AsyncClient,
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, "kept"))
    form = {"name": "intruder", "provider": "custom", "address": ADDRESS, "expected_name": "kept"}
    posts = [path for method, path in registry_requests(credential_id) if method == "POST"]

    plain = {path: (await admin_client.post(path, data=form)).status_code for path in posts}
    htmx = {
        path: (await admin_client.post(path, data=form, headers=HTMX)).status_code for path in posts
    }
    foreign = await admin_client.post(
        "/registries",
        data=anonymous_form(admin_client, "foreign"),
        headers=csrf_headers(admin_client) | {"Origin": "https://evil.example"},
    )
    listing = (await admin_client.get("/registries")).text

    assert set(plain.values()) == {FORBIDDEN}
    assert set(htmx.values()) == {FORBIDDEN}
    assert foreign.status_code == FORBIDDEN
    assert "kept" in listing
    assert "intruder" not in listing
    assert "foreign" not in listing


async def test_every_form_control_the_page_offers_is_submitted_by_a_browser_without_the_secret(
    admin_client: AsyncClient,
) -> None:
    page = (await admin_client.get("/registries/new", params={"provider": "gitlab"})).text

    submitted = form_values(page)

    assert {"provider", "address", "instance_url"} <= set(submitted)
    assert submitted["provider"] == "gitlab"
    assert "secret" in input_names(page)
    assert submitted.get("secret", "") == ""
