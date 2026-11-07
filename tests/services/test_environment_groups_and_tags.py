"""Environment groups and tags: naming rules, deletion with its confirmation, and activity."""

from collections.abc import Awaitable, Callable

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.environment import EnvironmentRepository
from app.services.assembly import AccessServices, EnvironmentServices
from app.services.environment_groups import (
    GROUP_DESCRIPTION_MAX,
    GROUP_NAME_MAX,
    GROUP_NAME_TAKEN,
    UNGROUP_NOTE,
)
from app.services.environments import CHANGED_SINCE_REVIEW, MAX_BATCH
from app.services.tags import TAG_NAME_MAX, TAG_NAME_TAKEN
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, team_of
from tests.auth_support import ADMIN_ID, STANDARD_ID, FakeClock
from tests.environment_support import activity_rows

ABSENT_ID = 9999


@pytest.fixture
def repository(db: Session, clock: FakeClock) -> EnvironmentRepository:
    return EnvironmentRepository(db, clock)


async def test_creating_a_group_trims_its_name_and_records_one_event(
    environments: EnvironmentServices, db: Session
) -> None:
    group = await environments.groups.create(ADMIN, name="  production  ", description=" live ")

    assert (group.name, group.description, group.environment_count) == ("production", "live", 0)
    [row] = await activity_rows(db)
    assert (row["action"], row["result"], row["actor_user_id"]) == (
        "environment_group_created",
        "success",
        ADMIN_ID,
    )
    assert (row["target_type"], row["target_id"], row["environment_id"]) == (
        "environment_group",
        str(group.id),
        None,
    )
    assert row["detail"] == {"name": "production"}


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x" * (GROUP_NAME_MAX + 1), "tab\there", "zero​width"],
)
async def test_a_group_name_that_is_empty_too_long_or_unprintable_is_refused(
    environments: EnvironmentServices, name: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.groups.create(ADMIN, name=name)

    assert list(refused.value.fields) == ["name"]


async def test_a_group_description_over_the_limit_is_refused(
    environments: EnvironmentServices,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.groups.create(
            ADMIN, name="production", description="x" * (GROUP_DESCRIPTION_MAX + 1)
        )

    assert list(refused.value.fields) == ["description"]


@pytest.mark.parametrize("repeat", ["production", "PRODUCTION", " Production "])
async def test_a_group_name_already_taken_in_any_case_is_refused(
    environments: EnvironmentServices, repeat: str
) -> None:
    await environments.groups.create(ADMIN, name="production")

    with pytest.raises(ValidationFailed) as refused:
        await environments.groups.create(ADMIN, name=repeat)

    assert refused.value.fields == {"name": GROUP_NAME_TAKEN}


async def test_renaming_a_group_records_the_previous_name_and_saving_no_change_records_nothing(
    environments: EnvironmentServices, db: Session
) -> None:
    group = await environments.groups.create(ADMIN, name="production")

    await environments.groups.update(ADMIN, group.id, name="production", description=None)
    await environments.groups.update(ADMIN, group.id, name="live", description=None)

    updated = [r for r in await activity_rows(db) if r["action"] == "environment_group_updated"]
    assert [row["detail"] for row in updated] == [{"previous_name": "production", "name": "live"}]


async def test_a_group_rename_to_another_groups_name_is_refused(
    environments: EnvironmentServices,
) -> None:
    await environments.groups.create(ADMIN, name="production")
    other = await environments.groups.create(ADMIN, name="staging")

    with pytest.raises(ValidationFailed):
        await environments.groups.update(ADMIN, other.id, name="Production", description=None)


async def test_assigning_environments_reports_each_one_and_records_one_event_per_move(
    environments: EnvironmentServices, repository: EnvironmentRepository, db: Session
) -> None:
    group = await environments.groups.create(ADMIN, name="production")

    result = await environments.groups.assign_environments(
        ADMIN, group.id, [ENV_A, ABSENT_ID, ENV_B]
    )

    assert [(item.environment_id, item.outcome) for item in result.items] == [
        (ENV_A, "assigned"),
        (ABSENT_ID, "not_found"),
        (ENV_B, "assigned"),
    ]
    moves = [r for r in await activity_rows(db) if r["action"] == "environment_group_assigned"]
    assert [row["target_id"] for row in moves] == [str(ENV_A), str(ENV_B)]
    assert (await environments.groups.get(ADMIN, group.id)).environment_count == 2


@pytest.mark.parametrize("ids", [[], list(range(1, MAX_BATCH + 2))], ids=["none", "over the cap"])
async def test_assigning_none_or_more_than_fifty_environments_is_refused(
    environments: EnvironmentServices, ids: list[int]
) -> None:
    group = await environments.groups.create(ADMIN, name="production")

    with pytest.raises(ValidationFailed) as refused:
        await environments.groups.assign_environments(ADMIN, group.id, ids)

    assert list(refused.value.fields) == ["environments"]


async def test_the_group_deletion_preview_lists_the_environments_it_ungroups_and_who_loses_access(
    environments: EnvironmentServices, access: AccessServices
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    await environments.groups.assign_environments(ADMIN, group.id, [ENV_A, ENV_B])
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)
    team_id = await team_of(access, "platform", STANDARD)
    await environments.groups.grant(ADMIN, group.id, team_id=team_id)

    preview = await environments.groups.delete_preview(ADMIN, group.id)

    assert [member.name for member in preview.environments] == ["environment-a", "environment-b"]
    assert preview.environments_total == 2
    assert [user.name for user in preview.grants.users] == ["standard-user"]
    assert [team.name for team in preview.grants.teams] == ["platform"]
    assert preview.standard_users_affected == 1
    assert preview.ungroup_note == UNGROUP_NOTE


async def test_deleting_a_group_ungroups_its_environments_and_drops_its_grants(
    environments: EnvironmentServices,
    repository: EnvironmentRepository,
    db: Session,
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    await environments.groups.assign_environments(ADMIN, group.id, [ENV_A])
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)

    await environments.groups.delete(ADMIN, group.id, expected_name="production")

    stored = await repository.get(ENV_A)
    assert stored is not None and stored.environment_group_id is None
    row = (await activity_rows(db))[-1]
    assert (row["action"], row["target_id"], row["detail"]) == (
        "environment_group_deleted",
        str(group.id),
        {"name": "production", "ungrouped": 1},
    )
    with pytest.raises(ResourceNotFound):
        await environments.groups.get(ADMIN, group.id)


async def test_a_group_delete_with_a_stale_name_deletes_nothing(
    environments: EnvironmentServices,
) -> None:
    group = await environments.groups.create(ADMIN, name="production")

    with pytest.raises(ValidationFailed) as refused:
        await environments.groups.delete(ADMIN, group.id, expected_name="staging")

    assert refused.value.fields == {"name": CHANGED_SINCE_REVIEW}
    assert (await environments.groups.get(ADMIN, group.id)).name == "production"


async def test_group_grants_are_recorded_by_grantee_and_a_repeat_is_refused(
    environments: EnvironmentServices, db: Session
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)

    with pytest.raises(ValidationFailed, match="already exists"):
        await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)
    await environments.groups.revoke(ADMIN, group.id, user_id=STANDARD_ID)

    rows = [r for r in await activity_rows(db) if "access" in r["action"]]
    assert [(r["action"], r["result"]) for r in rows] == [
        ("environment_group_access_granted", "success"),
        ("environment_group_access_granted", "failure"),
        ("environment_group_access_revoked", "success"),
    ]
    assert rows[0]["detail"] == {"grantee_type": "user", "grantee": "standard-user"}


async def test_the_access_view_of_an_environment_shows_what_it_inherits_from_its_group(
    environments: EnvironmentServices,
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    await environments.groups.assign_environments(ADMIN, group.id, [ENV_A])
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)
    await environments.environments.revoke(ADMIN, ENV_A, user_id=STANDARD_ID)

    view = await environments.access.for_environment(ADMIN, ENV_A)

    assert "standard-user" not in [user.name for user in view.direct.users]
    assert view.group is not None and view.group.name == "production"
    assert view.inherited is not None
    assert [user.name for user in view.inherited.users] == ["standard-user"]


async def test_a_grantee_cannot_be_an_administrator_or_an_unknown_user_or_team(
    environments: EnvironmentServices,
) -> None:
    with pytest.raises(ValidationFailed, match="need no grant"):
        await environments.access.resolve_grantee(ADMIN, username="admin-user")
    with pytest.raises(ValidationFailed, match="No user"):
        await environments.access.resolve_grantee(ADMIN, username="nobody")
    with pytest.raises(ValidationFailed, match="No team"):
        await environments.access.resolve_grantee(ADMIN, team_name="nobody")


@pytest.mark.parametrize(
    "operation",
    [
        lambda services: services.groups.create(STANDARD, name="x"),
        lambda services: services.groups.list_page(STANDARD, limit=10),
        lambda services: services.groups.delete_preview(STANDARD, 1),
        lambda services: services.tags.create(STANDARD, name="x"),
        lambda services: services.tags.list_page(STANDARD, limit=10),
        lambda services: services.access.for_environment(STANDARD, ENV_A),
    ],
    ids=["group create", "group list", "group preview", "tag create", "tag list", "access view"],
)
async def test_groups_tags_and_access_views_are_for_administrators_only(
    environments: EnvironmentServices,
    db: Session,
    operation: Callable[[EnvironmentServices], Awaitable[object]],
) -> None:
    with pytest.raises(PermissionDenied):
        await operation(environments)

    assert await activity_rows(db) == []


async def test_creating_a_tag_records_one_event_and_the_name_is_unique_in_any_case(
    environments: EnvironmentServices, db: Session
) -> None:
    tag = await environments.tags.create(ADMIN, name=" Blue ")

    with pytest.raises(ValidationFailed) as refused:
        await environments.tags.create(ADMIN, name="blue")

    assert tag.name == "Blue"
    assert refused.value.fields == {"name": TAG_NAME_TAKEN}
    rows = await activity_rows(db)
    assert [(r["action"], r["result"]) for r in rows] == [
        ("tag_created", "success"),
        ("tag_created", "failure"),
    ]
    assert (rows[0]["target_type"], rows[0]["target_id"]) == ("tag", str(tag.id))


@pytest.mark.parametrize(
    "name", ["", "x" * (TAG_NAME_MAX + 1), "has/slash", "semi;colon", "ünïcode", "new\nline"]
)
async def test_a_tag_name_outside_the_allowed_characters_is_refused(
    environments: EnvironmentServices, name: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.tags.create(ADMIN, name=name)

    assert list(refused.value.fields) == ["name"]


async def test_the_tag_deletion_preview_names_the_environments_that_lose_it(
    environments: EnvironmentServices,
) -> None:
    tag = await environments.tags.create(ADMIN, name="blue")
    await environments.environments.set_tags(ADMIN, ENV_A, [tag.id])

    preview = await environments.tags.delete_preview(ADMIN, tag.id)

    assert [member.name for member in preview.environments] == ["environment-a"]
    assert preview.environments_total == 1


async def test_deleting_a_tag_removes_its_links_and_records_how_many_environments_carried_it(
    environments: EnvironmentServices, db: Session
) -> None:
    tag = await environments.tags.create(ADMIN, name="blue")
    await environments.environments.set_tags(ADMIN, ENV_A, [tag.id])

    await environments.tags.delete(ADMIN, tag.id, expected_name="blue")

    detail = await environments.environments.get(ADMIN, ENV_A)
    assert detail.summary.tags == ()
    row = (await activity_rows(db))[-1]
    assert (row["action"], row["detail"]) == ("tag_deleted", {"name": "blue", "environments": 1})


async def test_a_tag_delete_with_a_stale_name_deletes_nothing(
    environments: EnvironmentServices,
) -> None:
    tag = await environments.tags.create(ADMIN, name="blue")

    with pytest.raises(ValidationFailed) as refused:
        await environments.tags.delete(ADMIN, tag.id, expected_name="green")

    assert refused.value.fields == {"name": CHANGED_SINCE_REVIEW}
    page = await environments.tags.list_page(ADMIN, limit=10)
    assert [summary.name for summary in page.items] == ["blue"]
