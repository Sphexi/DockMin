"""The team pages: each change redirects to the team's page with a notice, a refusal re-renders."""

import re

from httpx import AsyncClient, Response

from tests.auth_support import STANDARD_ID, STANDARD_USERNAME, csrf_form
from tests.web.flash_support import flash_texts, land, landed_texts, location

TEAM_NAME = "platform"
RENAMED = "infrastructure"
SEE_OTHER = 303
UNPROCESSABLE = 422
TEAM_PATH = re.compile(r"/teams/(\d+)")


def team_id_of(response: Response) -> int:
    found = TEAM_PATH.fullmatch(location(response))
    assert found is not None, location(response)
    return int(found.group(1))


async def test_each_change_to_a_team_redirects_to_its_page_with_a_notice(
    admin_client: AsyncClient,
) -> None:
    created = await admin_client.post("/teams", data=csrf_form(admin_client, name=TEAM_NAME))
    team = f"/teams/{team_id_of(created)}"
    created_notices = await landed_texts(admin_client, created)
    renamed = await admin_client.post(team, data=csrf_form(admin_client, name=RENAMED))
    renamed_notices = await landed_texts(admin_client, renamed)
    added = await admin_client.post(
        f"{team}/members", data=csrf_form(admin_client, username=STANDARD_USERNAME)
    )
    added_notices = await landed_texts(admin_client, added)
    appointed = await admin_client.post(
        f"{team}/members/{STANDARD_ID}/leader", data=csrf_form(admin_client, is_leader="true")
    )
    appointed_notices = await landed_texts(admin_client, appointed)
    dismissed = await admin_client.post(
        f"{team}/members/{STANDARD_ID}/leader", data=csrf_form(admin_client, is_leader="false")
    )
    dismissed_notices = await landed_texts(admin_client, dismissed)
    removed = await admin_client.post(
        f"{team}/members/{STANDARD_ID}/remove", data=csrf_form(admin_client)
    )
    removed_notices = await landed_texts(admin_client, removed)

    assert [location(r) for r in (renamed, added, appointed, dismissed, removed)] == [team] * 5
    assert created_notices == ["Team created."]
    assert renamed_notices == ["Team renamed."]
    assert added_notices == [f"Added {STANDARD_USERNAME} to the team."]
    assert appointed_notices == ["Leader appointed."]
    assert dismissed_notices == ["Leader dismissed."]
    assert removed_notices == ["Member removed."]


async def test_a_refused_team_form_renders_with_its_errors_and_leaves_no_notice_behind(
    admin_client: AsyncClient,
) -> None:
    created = await admin_client.post("/teams", data=csrf_form(admin_client, name=TEAM_NAME))
    team = f"/teams/{team_id_of(created)}"
    await land(admin_client, created)

    blank = await admin_client.post("/teams", data=csrf_form(admin_client, name=" "))
    unknown = await admin_client.post(
        f"{team}/members", data=csrf_form(admin_client, username="nobody")
    )
    page = await admin_client.get(team)

    assert (blank.status_code, unknown.status_code) == (UNPROCESSABLE, UNPROCESSABLE)
    assert flash_texts(page.text) == []
