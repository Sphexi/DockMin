"""Converted identity and administration posts answer 303 and a notice; refusals still render."""

from httpx import AsyncClient

from tests.auth_support import STANDARD_ID, STANDARD_USERNAME, csrf_form
from tests.web.environment_support import World, hold_unreachable
from tests.web.flash_support import SEE_OTHER, flash_cookie_of, flash_texts, land, location

UNPROCESSABLE = 422
RENAMED = "standard-renamed"
GROUP_RENAMED = "production-renamed"
SESSIONS_ENDED = "Ended 0 session(s) and revoked the user's API tokens and webhook URLs."


async def post_and_land(
    client: AsyncClient, path: str, **fields: str
) -> tuple[int, str, list[str]]:
    """Post a form; return the status, the redirect target and the notices the target shows."""
    response = await client.post(path, data=csrf_form(client, **fields))
    landing = await land(client, response)
    return response.status_code, location(response), flash_texts(landing.text)


async def test_each_converted_post_redirects_to_the_page_it_changed_and_shows_one_notice_there(
    admin_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.remote_id)
    user = f"/users/{STANDARD_ID}"
    group = f"/groups/{world.group_id}"
    environment = f"/environments/{world.remote_id}"
    group_access = f"{group}?open=access#access"
    environment_access = f"{environment}?open=access#access"
    done = {
        "user update": await post_and_land(admin_client, user, username=RENAMED, role="standard"),
        "user disable": await post_and_land(admin_client, f"{user}/disable"),
        "user enable": await post_and_land(admin_client, f"{user}/enable"),
        "user sessions": await post_and_land(admin_client, f"{user}/sessions/revoke"),
        "group edit": await post_and_land(admin_client, f"{group}/edit", name=GROUP_RENAMED),
        "group environments": await post_and_land(
            admin_client, f"{group}/environments", environment=str(world.remote_id)
        ),
        "group grant": await post_and_land(
            admin_client, f"{group}/access", grantee_kind="user", grantee_name=RENAMED
        ),
        "group revoke": await post_and_land(
            admin_client, f"{group}/access/remove?user_id={STANDARD_ID}"
        ),
        "environment refresh": await post_and_land(admin_client, f"{environment}/refresh"),
        "environment grant": await post_and_land(
            admin_client, f"{environment}/access", grantee_kind="user", grantee_name=RENAMED
        ),
        "environment revoke": await post_and_land(
            admin_client, f"{environment}/access/remove?user_id={STANDARD_ID}"
        ),
        "tag": await post_and_land(admin_client, "/tags", name="red"),
    }

    assert done == {
        "user update": (SEE_OTHER, user, ["User updated."]),
        "user disable": (SEE_OTHER, user, ["User disabled."]),
        "user enable": (SEE_OTHER, user, ["User enabled."]),
        "user sessions": (SEE_OTHER, user, [SESSIONS_ENDED]),
        "group edit": (SEE_OTHER, group, ["Group saved."]),
        "group environments": (SEE_OTHER, group, ["Moved 1 environment into the group."]),
        "group grant": (SEE_OTHER, group_access, ["Access granted."]),
        "group revoke": (SEE_OTHER, group_access, ["Access removed."]),
        "environment refresh": (
            SEE_OTHER,
            environment,
            ["Refresh requested. The counts update shortly."],
        ),
        "environment grant": (SEE_OTHER, environment_access, ["Access granted."]),
        "environment revoke": (SEE_OTHER, environment_access, ["Access removed."]),
        "tag": (SEE_OTHER, "/tags", ["Created the tag red."]),
    }


async def test_a_refused_post_renders_its_form_at_422_with_no_redirect_and_no_notice(
    admin_client: AsyncClient, world: World
) -> None:
    await hold_unreachable(world, world.remote_id)
    refusals = {
        "user role": (f"/users/{STANDARD_ID}", {"username": STANDARD_USERNAME, "role": "boss"}),
        "group name": (f"/groups/{world.group_id}/edit", {"name": " "}),
        "group new": ("/groups/new", {"name": " "}),
        "tag": ("/tags", {"name": "a/b"}),
        "environment grantee": (
            f"/environments/{world.remote_id}/access",
            {"grantee_kind": "user", "grantee_name": "nobody"},
        ),
    }

    responses = {
        name: await admin_client.post(path, data=csrf_form(admin_client, **fields))
        for name, (path, fields) in refusals.items()
    }

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(
        refusals, UNPROCESSABLE
    )
    assert {name: "location" in r.headers for name, r in responses.items()} == dict.fromkeys(
        refusals, False
    )
    assert flash_cookie_of(admin_client) is None
