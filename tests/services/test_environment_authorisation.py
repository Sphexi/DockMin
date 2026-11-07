"""The environment gate composed in front of the per-object controls."""

import sqlite3
from collections.abc import Awaitable, Callable

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.environment_access import (
    EnvironmentAccessRepository,
    environment_visible_clause,
)
from app.services.access import Reason, ResourceKey
from app.services.assembly import AccessServices, EnvironmentServices
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    MEMBER,
    STANDARD,
    TEAM_NAME,
    UNKNOWN_USER_ID,
    FakeInspector,
    ListedObject,
    container_key,
    control,
    team_of,
    volume_key,
)
from tests.auth_support import STANDARD_ID

GROUP_NAME = "production"
OTHER_GROUP_NAME = "staging"
MISSING_ENVIRONMENT = 9999

Grant = Callable[[AccessServices, EnvironmentServices], Awaitable[None]]


async def make_public(access: AccessServices, key: ResourceKey) -> None:
    await control(access, key, public=True)


async def grant_user(access: AccessServices, environments: EnvironmentServices) -> None:
    await environments.environments.grant(ADMIN, ENV_A, user_id=STANDARD_ID)


async def grant_team(access: AccessServices, environments: EnvironmentServices) -> None:
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    await environments.environments.grant(ADMIN, ENV_A, team_id=team_id)


async def grant_group_to_user(access: AccessServices, environments: EnvironmentServices) -> None:
    group = await environments.groups.create(ADMIN, name=GROUP_NAME)
    await environments.environments.assign_group(ADMIN, ENV_A, group.id)
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)


async def grant_group_to_team(access: AccessServices, environments: EnvironmentServices) -> None:
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    group = await environments.groups.create(ADMIN, name=GROUP_NAME)
    await environments.environments.assign_group(ADMIN, ENV_A, group.id)
    await environments.groups.grant(ADMIN, group.id, team_id=team_id)


GRANTS: dict[str, Grant] = {
    "user": grant_user,
    "team": grant_team,
    "group user": grant_group_to_user,
    "group team": grant_group_to_team,
}


async def test_an_administrator_reaches_every_environment_without_any_grant(
    access: AccessServices, no_grants: None
) -> None:
    assert await access.authoriser.environment_visible(ADMIN, ENV_A)
    assert await access.authoriser.environment_visible(ADMIN, ENV_B)


async def test_a_standard_user_without_a_grant_is_denied_the_environment(
    access: AccessServices, no_grants: None
) -> None:
    assert not await access.authoriser.environment_visible(STANDARD, ENV_A)


async def test_a_missing_environment_and_a_denied_one_are_refused_identically(
    access: AccessServices, no_grants: None
) -> None:
    with pytest.raises(ResourceNotFound) as denied:
        await access.authoriser.require_environment(STANDARD, ENV_A)
    with pytest.raises(ResourceNotFound) as missing:
        await access.authoriser.require_environment(STANDARD, MISSING_ENVIRONMENT)

    assert str(denied.value) == str(missing.value)


async def test_a_public_object_in_a_denied_environment_is_not_found_and_docker_is_not_asked(
    access: AccessServices, inspector: FakeInspector, no_grants: None
) -> None:
    key = container_key(1)
    inspector.add(key)
    await make_public(access, key)

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, key)

    assert inspector.calls == []


async def test_a_public_object_is_reachable_once_the_environment_is_granted(
    access: AccessServices,
    inspector: FakeInspector,
    new_request: Callable[[], AccessServices],
    db: Session,
    no_grants: None,
) -> None:
    key = container_key(1)
    inspector.add(key)
    await make_public(access, key)
    await EnvironmentAccessRepository(db).add_user("environment", ENV_A, STANDARD_ID)

    await new_request().authoriser.require_access(STANDARD, key)

    await access.authoriser.require_access(STANDARD, key)


async def test_a_listing_omits_objects_of_a_denied_environment_and_keeps_the_rest(
    access: AccessServices, db: Session, no_grants: None
) -> None:
    denied, granted = volume_key("data", ENV_A), volume_key("data", ENV_B)
    await make_public(access, denied)
    await make_public(access, granted)
    await EnvironmentAccessRepository(db).add_user("environment", ENV_B, STANDARD_ID)
    listing = [ListedObject.of(denied), ListedObject.of(granted)]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == [listing[1]]


async def stock_item(db: Session, access: AccessServices, environment_id: int) -> None:
    await db.execute("INSERT INTO item VALUES (?, 'data')", (environment_id,))
    await make_public(access, volume_key("data", environment_id))


async def admitted_rows(
    db: Session, access: AccessServices, environment_id: int
) -> list[sqlite3.Row]:
    visibility = await access.authoriser.resolve_visibility(STANDARD, environment_id, "volume")
    fragment = visibility.sql_filter("item.name", "item.host")
    return await db.fetch_limited(
        f"SELECT host FROM item WHERE item.host = ? AND {fragment.clause}",
        (environment_id, *fragment.params),
        limit=10,
    )


async def test_the_sql_filter_for_a_denied_environment_admits_nothing_and_for_a_granted_one_does(
    access: AccessServices, db: Session, no_grants: None
) -> None:
    await db.execute("CREATE TABLE item (host INTEGER NOT NULL, name TEXT NOT NULL)")
    await stock_item(db, access, ENV_A)
    await stock_item(db, access, ENV_B)
    await EnvironmentAccessRepository(db).add_user("environment", ENV_B, STANDARD_ID)

    denied = await admitted_rows(db, access, ENV_A)
    granted = await admitted_rows(db, access, ENV_B)

    assert (len(denied), len(granted)) == (0, 1)


@pytest.mark.parametrize("grant", GRANTS.values(), ids=GRANTS.keys())
async def test_each_kind_of_grant_makes_the_environment_visible_to_the_grantee_only(
    access: AccessServices,
    environments: EnvironmentServices,
    new_request: Callable[[], AccessServices],
    no_grants: None,
    grant: Grant,
) -> None:
    await grant(access, environments)

    later = new_request().authoriser

    assert await later.environment_visible(STANDARD, ENV_A)
    assert not await later.environment_visible(MEMBER, ENV_A)
    assert not await later.environment_visible(STANDARD, ENV_B)


async def revoke_user(access: AccessServices, environments: EnvironmentServices) -> None:
    await environments.environments.revoke(ADMIN, ENV_A, user_id=STANDARD_ID)


async def test_revoking_a_direct_grant_takes_effect_for_the_next_request(
    access: AccessServices,
    environments: EnvironmentServices,
    new_request: Callable[[], AccessServices],
    no_grants: None,
) -> None:
    await grant_user(access, environments)
    assert await new_request().authoriser.environment_visible(STANDARD, ENV_A)

    await revoke_user(access, environments)

    assert not await new_request().authoriser.environment_visible(STANDARD, ENV_A)


async def test_removing_a_user_from_the_granted_team_takes_effect_for_the_next_request(
    access: AccessServices,
    environments: EnvironmentServices,
    new_request: Callable[[], AccessServices],
    no_grants: None,
) -> None:
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    await environments.environments.grant(ADMIN, ENV_A, team_id=team_id)
    assert await new_request().authoriser.environment_visible(STANDARD, ENV_A)

    await access.teams.remove_member(ADMIN, team_id, user_id=STANDARD_ID)

    assert not await new_request().authoriser.environment_visible(STANDARD, ENV_A)


async def test_moving_an_environment_out_of_a_granted_group_takes_effect_for_the_next_request(
    access: AccessServices,
    environments: EnvironmentServices,
    new_request: Callable[[], AccessServices],
    no_grants: None,
) -> None:
    await grant_group_to_user(access, environments)
    other = await environments.groups.create(ADMIN, name=OTHER_GROUP_NAME)
    assert await new_request().authoriser.environment_visible(STANDARD, ENV_A)

    await environments.environments.assign_group(ADMIN, ENV_A, other.id)

    assert not await new_request().authoriser.environment_visible(STANDARD, ENV_A)


async def test_a_group_grant_does_not_reach_environments_outside_the_group(
    access: AccessServices,
    environments: EnvironmentServices,
    new_request: Callable[[], AccessServices],
    no_grants: None,
) -> None:
    await grant_group_to_user(access, environments)

    assert not await new_request().authoriser.environment_visible(STANDARD, ENV_B)


async def test_an_environment_decision_is_remembered_for_the_rest_of_the_request(
    access: AccessServices, environments: EnvironmentServices, no_grants: None
) -> None:
    await grant_user(access, environments)
    assert await access.authoriser.environment_visible(STANDARD, ENV_A)

    await revoke_user(access, environments)

    assert await access.authoriser.environment_visible(STANDARD, ENV_A)


async def test_the_explanation_names_the_environment_gate_as_the_reason_for_a_denied_user(
    access: AccessServices, no_grants: None
) -> None:
    key = container_key(1)
    await make_public(access, key)

    explained = await access.effective.explain(
        ADMIN, STANDARD_ID, ENV_A, "container", [ListedObject.of(key)]
    )

    assert explained.environment_access is False
    assert explained.entries[0].level is None
    assert explained.entries[0].reasons == (Reason.NO_ENVIRONMENT_ACCESS,)


async def test_the_explanation_of_a_granted_user_reaches_the_object_level_reason(
    access: AccessServices, environments: EnvironmentServices, no_grants: None
) -> None:
    key = container_key(1)
    await make_public(access, key)
    await grant_user(access, environments)

    explained = await access.effective.explain(
        ADMIN, STANDARD_ID, ENV_A, "container", [ListedObject.of(key)]
    )

    assert explained.environment_access is True
    assert explained.entries[0].reasons == (Reason.PUBLIC,)


@pytest.mark.parametrize(
    "operation",
    [
        lambda services: services.environments.grant(STANDARD, ENV_A, user_id=STANDARD_ID),
        lambda services: services.environments.revoke(STANDARD, ENV_A, user_id=STANDARD_ID),
        lambda services: services.environments.refresh(STANDARD, ENV_A),
        lambda services: services.environments.test_connection(STANDARD, ENV_A),
        lambda services: services.environments.delete(STANDARD, ENV_A, expected_name="x"),
        lambda services: services.environments.assign_group(STANDARD, ENV_A, None),
        lambda services: services.environments.set_tags(STANDARD, ENV_A, []),
    ],
    ids=["grant", "revoke", "refresh", "test", "delete", "assign_group", "set_tags"],
)
async def test_a_standard_user_who_can_see_an_environment_is_forbidden_its_administrator_actions(
    environments: EnvironmentServices, operation: Callable[[EnvironmentServices], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionDenied):
        await operation(environments)


@pytest.mark.parametrize(
    "operation",
    [
        lambda services: services.environments.grant(STANDARD, ENV_A, user_id=STANDARD_ID),
        lambda services: services.environments.refresh(STANDARD, ENV_A),
        lambda services: services.environments.test_connection(STANDARD, ENV_A),
        lambda services: services.environments.delete(STANDARD, ENV_A, expected_name="x"),
        lambda services: services.environments.get(STANDARD, ENV_A),
    ],
    ids=["grant", "refresh", "test", "delete", "get"],
)
async def test_a_standard_user_without_a_grant_gets_not_found_rather_than_forbidden(
    environments: EnvironmentServices,
    no_grants: None,
    operation: Callable[[EnvironmentServices], Awaitable[object]],
) -> None:
    with pytest.raises(ResourceNotFound):
        await operation(environments)


async def test_an_unknown_user_id_is_not_granted_an_environment(
    environments: EnvironmentServices,
) -> None:
    with pytest.raises(ValidationFailed, match="no longer exists"):
        await environments.environments.grant(ADMIN, ENV_A, user_id=UNKNOWN_USER_ID)


@pytest.mark.parametrize(
    ("environment_column", "group_column", "message"),
    [
        ("id", "e.environment_group_id", "table-qualified"),
        ("e.id", "environment_group_id", "table-qualified"),
        ("e.id) OR (1=1", "e.environment_group_id", "table-qualified"),
        ("eua.id", "e.environment_group_id", "reserves"),
        ("e.id", "tm.environment_group_id", "reserves"),
    ],
    ids=[
        "unqualified environment",
        "unqualified group",
        "injected",
        "reserved alias",
        "reserved tm",
    ],
)
def test_the_visibility_clause_refuses_a_column_that_would_be_captured_by_its_own_subqueries(
    environment_column: str, group_column: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        environment_visible_clause(STANDARD_ID, environment_column, group_column)
