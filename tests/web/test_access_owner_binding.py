"""The access panel binds its owner field to the account id it showed."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.auth.errors import ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Database
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.services.access import ResourceKey
from app.services.grantees import OWNER_GONE, GranteeDirectory
from app.services.resource_controls import (
    CHANGED_REASON,
    ControlView,
    ResourceControlService,
    control_digest,
)
from app.web.object_pages import AccessInput, write_access
from tests.auth_support import STANDARD_ID, STANDARD_USERNAME
from tests.db.helpers import PASSWORD_HASH
from tests.docker_engine.container_engine import ContainerEngine
from tests.web.access_panel_support import (
    CONFLICT,
    SEE_OTHER,
    PanelRoutes,
    activity_details,
    digest_in,
    save,
)
from tests.web.environment_support import World
from tests.web.helpers import hidden_inputs
from tests.web.test_container_pages import access_routes

OTHER_ACCOUNT = "account-b"
RENAMED = "renamed-account"
GONE_ID = 9999


async def owner_of(app: FastAPI) -> int | None:
    async with app.state.database.session() as db:
        row = await db.fetch_one("SELECT owner_user_id FROM resource_control")
    assert row is not None
    owner = row["owner_user_id"]
    return None if owner is None else int(owner)


async def swap_names(app: FastAPI) -> int:
    """Create a second account, then rename the owner away and give its old name to the second."""
    async with app.state.database.session() as db:
        users = UserRepository(db)
        other = await users.create(OTHER_ACCOUNT, PASSWORD_HASH, "standard")
        await users.set_username(STANDARD_ID, RENAMED)
        await users.set_username(other.id, STANDARD_USERNAME)
    return other.id


async def own_and_review(client: AsyncClient, routes: PanelRoutes) -> tuple[str, dict[str, str]]:
    """Make the standard user the owner, then return the digest and hidden fields of the panel."""
    empty = await client.get(routes.page)
    saved = await save(
        client,
        routes,
        digest_in(empty.text),
        visibility="restricted",
        access_level="read",
        owner_name=STANDARD_USERNAME,
    )
    assert saved.status_code == SEE_OTHER
    reviewed = await client.get(routes.page)
    return digest_in(reviewed.text), hidden_inputs(reviewed.text)


async def test_a_rename_swap_keeps_the_reviewed_owner_and_a_typed_name_still_resolves(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = access_routes(world)
    digest, hidden = await own_and_review(admin_client, routes)
    other_id = await swap_names(world.app)

    kept = await save(
        admin_client,
        routes,
        digest,
        visibility="restricted",
        access_level="read",
        owner_name=hidden["owner_name_shown"],
        owner_user_id=hidden["owner_user_id"],
        owner_name_shown=hidden["owner_name_shown"],
    )
    owner_after_keep = await owner_of(world.app)
    typed = await save(
        admin_client,
        routes,
        digest,
        visibility="restricted",
        access_level="read",
        owner_name=STANDARD_USERNAME,
        owner_user_id=hidden["owner_user_id"],
        owner_name_shown=RENAMED,
    )

    assert hidden["owner_user_id"] == str(STANDARD_ID)
    assert hidden["owner_name_shown"] == STANDARD_USERNAME
    assert kept.status_code == SEE_OTHER
    assert owner_after_keep == STANDARD_ID
    assert typed.status_code == SEE_OTHER
    assert await owner_of(world.app) == other_id


async def test_a_tampered_owner_id_under_an_unchanged_name_is_a_conflict_that_records_the_refusal(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    routes = access_routes(world)
    digest, hidden = await own_and_review(admin_client, routes)
    other_id = await swap_names(world.app)

    other_account = await save(
        admin_client,
        routes,
        digest,
        visibility="restricted",
        access_level="read",
        owner_name=hidden["owner_name_shown"],
        owner_user_id=str(other_id),
        owner_name_shown=hidden["owner_name_shown"],
    )
    not_an_id = await save(
        admin_client,
        routes,
        digest,
        visibility="restricted",
        access_level="read",
        owner_name=hidden["owner_name_shown"],
        owner_user_id="1 OR 1",
        owner_name_shown=hidden["owner_name_shown"],
    )
    failures = await activity_details(world.app, "resource_control_updated", "failure")

    assert (other_account.status_code, not_an_id.status_code) == (CONFLICT, CONFLICT)
    assert await owner_of(world.app) == STANDARD_ID
    assert [row["reason"] for row in failures] == [CHANGED_REASON] * 2


class ControlsNotReached:
    async def update(self, *_: Any, **__: Any) -> None:
        raise AssertionError("the form was refused before the control was written")


async def test_an_owner_deleted_between_review_and_save_is_a_422_on_the_owner_field(
    database: Database,
) -> None:
    key = ResourceKey(1, "container", "web")
    view = ControlView(
        id=1,
        key=key,
        is_public=False,
        is_administrators_only=False,
        access_level="read",
        owner_user_id=GONE_ID,
        user_ids=(),
        team_ids=(),
        sub_resource_ids=(),
        updated_at="2030-01-01T00:00:00Z",
    )
    data = AccessInput(
        visibility="restricted",
        access_level="read",
        owner_name="vanished",
        keep_user_ids=[],
        keep_team_ids=[],
        add_users="",
        add_teams="",
        expected_digest=control_digest(view),
        owner_user_id=str(GONE_ID),
        owner_name_shown="vanished",
    )
    async with database.session() as session:
        services = SimpleNamespace(
            grantees=GranteeDirectory(UserRepository(session), TeamRepository(session))
        )
        with pytest.raises(ValidationFailed) as refused:
            await write_access(
                cast(Principal, None),
                cast(ResourceControlService, ControlsNotReached()),
                services,
                key,
                view,
                data,
            )

    assert refused.value.fields == {"owner_name": OWNER_GONE}
