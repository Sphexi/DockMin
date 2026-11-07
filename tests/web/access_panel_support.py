"""Driving the ownership and access panel of one object over its pages."""

import json
import re
from typing import Any, NamedTuple

from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.services.resource_controls import CHANGED_REASON
from tests.auth_support import STANDARD_USERNAME, csrf_form
from tests.web.flash_support import land

CONFLICT = 409
UNPROCESSABLE = 422
SEE_OTHER = 303
COMMA_TEAM = "ops, east"
OTHER_TEAM = "support"
UNKNOWN_USER = "nobody-by-that-name"
DIGEST = re.compile(r'name="expected_digest" value="([^"]*)"')
KEPT_USERS = re.compile(r'name="keep_user_ids" value="(\d+)" checked')
KEPT_TEAMS = re.compile(r'name="keep_team_ids" value="(\d+)" checked')
UNKNOWN_USER_TEXT = f"No user is named “{UNKNOWN_USER}”"


class PanelRoutes(NamedTuple):
    """Where one object's access tab is read and posted."""

    page: str
    save: str
    delete: str


def digest_in(html: str) -> str:
    """Return the digest the panel's save form posts back."""
    found = DIGEST.search(html)
    assert found is not None, "no access form on the page"
    return found.group(1)


def kept_users(html: str) -> list[int]:
    return [int(value) for value in KEPT_USERS.findall(html)]


def kept_teams(html: str) -> list[int]:
    return [int(value) for value in KEPT_TEAMS.findall(html)]


async def create_team(client: AsyncClient, name: str) -> None:
    """Create a team through its page."""
    created = await client.post("/teams", data=csrf_form(client, name=name))
    assert created.status_code == SEE_OTHER, created.text


async def activity_details(app: FastAPI, action: str, result: str) -> list[dict[str, Any]]:
    """Return the stored detail of each activity row of the action and result, oldest first."""
    async with app.state.database.session() as db:
        rows = await db.fetch_limited(
            "SELECT detail FROM activity_event WHERE action = ? AND result = ? ORDER BY id",
            (action, result),
            limit=100,
        )
    return [json.loads(row["detail"] or "{}") for row in rows]


async def save(client: AsyncClient, routes: PanelRoutes, digest: str, **fields: Any) -> Response:
    """Post the access form with the digest it was reviewed under."""
    return await client.post(routes.save, data=csrf_form(client, expected_digest=digest, **fields))


async def saved_page(
    client: AsyncClient, routes: PanelRoutes, digest: str, **fields: Any
) -> Response:
    """Post the access form, expect the redirect, and return the page it lands on."""
    return await land(client, await save(client, routes, digest, **fields))


async def grant_by_name_and_keep(
    client: AsyncClient, routes: PanelRoutes
) -> tuple[Response, Response]:
    """Grant a user and two teams by typed name, then save again keeping them; return both pages.

    A team name holding a comma is typed on a line of its own.
    """
    await create_team(client, COMMA_TEAM)
    await create_team(client, OTHER_TEAM)
    empty = await client.get(routes.page)
    granted = await saved_page(
        client,
        routes,
        digest_in(empty.text),
        visibility="restricted",
        access_level="write",
        owner_name=STANDARD_USERNAME,
        add_users=STANDARD_USERNAME,
        add_teams=f"{COMMA_TEAM}\n{OTHER_TEAM}",
    )
    resaved = await saved_page(
        client,
        routes,
        digest_in(granted.text),
        visibility="restricted",
        access_level="write",
        owner_name=STANDARD_USERNAME,
        keep_user_ids=[str(user) for user in kept_users(granted.text)],
        keep_team_ids=[str(team) for team in kept_teams(granted.text)],
    )
    return granted, resaved


class StaleSaves(NamedTuple):
    """The answers to saves made under a stale digest."""

    first_digest: str
    current_digest: str
    stale: Response
    stale_unknown_name: Response
    refused: Response


async def save_under_stale_digest(client: AsyncClient, routes: PanelRoutes) -> StaleSaves:
    """Change a control twice, then save under the first digest and under the current one."""
    empty = await client.get(routes.page)
    first = await saved_page(
        client, routes, digest_in(empty.text), visibility="public", access_level="read"
    )
    second = await saved_page(
        client,
        routes,
        digest_in(first.text),
        visibility="administrators_only",
        access_level="read",
    )
    first_digest, current_digest = digest_in(first.text), digest_in(second.text)
    stale = await save(client, routes, first_digest, visibility="restricted", access_level="write")
    stale_unknown_name = await save(
        client,
        routes,
        first_digest,
        visibility="restricted",
        access_level="read",
        add_users=UNKNOWN_USER,
    )
    refused = await save(
        client,
        routes,
        current_digest,
        visibility="restricted",
        access_level="read",
        add_users=UNKNOWN_USER,
    )
    return StaleSaves(first_digest, current_digest, stale, stale_unknown_name, refused)


def assert_grants_kept(granted: Response, resaved: Response) -> None:
    """Check that the user and both teams were granted by name and a later save kept all three."""
    assert len(kept_users(granted.text)) == 1 and len(kept_teams(granted.text)) == 2
    assert kept_users(resaved.text) == kept_users(granted.text)
    assert kept_teams(resaved.text) == kept_teams(granted.text)
    assert all(name in resaved.text for name in (STANDARD_USERNAME, COMMA_TEAM, OTHER_TEAM))


async def assert_stale_saves_refused(app: FastAPI, saves: StaleSaves) -> None:
    """Check that stale digests answer 409 with the current control, and a bad name 422."""
    failures = await activity_details(app, "resource_control_updated", "failure")
    assert (
        saves.stale.status_code,
        saves.stale_unknown_name.status_code,
        saves.refused.status_code,
    ) == (CONFLICT, CONFLICT, UNPROCESSABLE)
    assert saves.current_digest != saves.first_digest
    assert digest_in(saves.stale.text) == saves.current_digest
    assert "Administrators only" in saves.stale.text
    assert UNKNOWN_USER_TEXT not in saves.stale_unknown_name.text
    assert UNKNOWN_USER_TEXT in saves.refused.text
    assert digest_in(saves.refused.text) == saves.current_digest
    assert [row["reason"] for row in failures] == [CHANGED_REASON] * 2
