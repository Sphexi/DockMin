"""Tests for the six entity repositories under app/db/repositories/."""

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any

import pytest

from app.db.engine import Session
from app.db.repositories import (
    MAX_PAGE_SIZE,
    DuplicateError,
    EnvironmentGroup,
    EnvironmentGroupRepository,
    InvalidCursorError,
    MissingReferenceError,
    Page,
    SettingRepository,
    TagRepository,
    TeamMembershipRepository,
    TeamRepository,
    User,
    UserHasActivityError,
    UserRepository,
)
from tests.db.helpers import (
    ACTIVITY_INSERT,
    CREATED_AT,
    CREATED_STAMP,
    LATER_AT,
    LATER_STAMP,
    PASSWORD_HASH,
    UNKNOWN_ID,
    activity_row,
    at,
    walk_pages,
)

NAME = "Mixed-Case"
FIRST = "alpha"
SECOND = "bravo"
DESCRIPTION = "the primary hosts"
SETTING_KEY = "ui.theme"
UNSHIFTED_NAMES = ["bravo", "Alpha", "delta", "Charlie", "echo"]
CASE_INSENSITIVE_ORDER = ["Alpha", "bravo", "Charlie", "delta", "echo"]
TWO_HOURS = timezone(timedelta(hours=2))
LOCAL_MOMENT = datetime(2026, 1, 2, 5, 4, 5, tzinfo=TWO_HOURS)

ListPage = Callable[..., Awaitable[Page[Any]]]


async def seed_users(session: Session) -> ListPage:
    repo = UserRepository(session, clock=at(CREATED_AT))
    await repo.create(FIRST, PASSWORD_HASH, "standard")
    await repo.create(SECOND, PASSWORD_HASH, "standard")
    return repo.list_page


async def seed_teams(session: Session) -> ListPage:
    repo = TeamRepository(session, clock=at(CREATED_AT))
    await repo.create(FIRST)
    await repo.create(SECOND)
    return repo.list_page


async def seed_tags(session: Session) -> ListPage:
    repo = TagRepository(session, clock=at(CREATED_AT))
    await repo.create(FIRST)
    await repo.create(SECOND)
    return repo.list_page


async def seed_groups(session: Session) -> ListPage:
    repo = EnvironmentGroupRepository(session, clock=at(CREATED_AT))
    await repo.create(FIRST)
    await repo.create(SECOND)
    return repo.list_page


async def seed_settings(session: Session) -> ListPage:
    repo = SettingRepository(session, clock=at(CREATED_AT))
    await repo.put(FIRST, "string", "v")
    await repo.put(SECOND, "string", "v")
    return repo.list_page


async def seed_memberships(session: Session) -> tuple[Any, ...]:
    """Add two users to one team; return the repository, the team and the first user."""
    users = UserRepository(session, clock=at(CREATED_AT))
    team = await TeamRepository(session, clock=at(CREATED_AT)).create(NAME)
    first = await users.create(FIRST, PASSWORD_HASH, "standard")
    second = await users.create(SECOND, PASSWORD_HASH, "standard")
    repo = TeamMembershipRepository(session, clock=at(CREATED_AT))
    await repo.add(team.id, first.id)
    await repo.add(team.id, second.id)
    return repo, team, first


async def seed_team_memberships(session: Session) -> ListPage:
    repo, team, _ = await seed_memberships(session)
    return partial(repo.list_for_team, team.id)


async def seed_two_teams_for_one_user(session: Session) -> ListPage:
    teams = TeamRepository(session, clock=at(CREATED_AT))
    user = await UserRepository(session, clock=at(CREATED_AT)).create(
        FIRST, PASSWORD_HASH, "standard"
    )
    repo = TeamMembershipRepository(session, clock=at(CREATED_AT))
    await repo.add((await teams.create(FIRST)).id, user.id)
    await repo.add((await teams.create(SECOND)).id, user.id)
    return partial(repo.list_for_user, user.id)


LIST_SEEDS = [
    pytest.param(seed_users, id="user"),
    pytest.param(seed_teams, id="team"),
    pytest.param(seed_tags, id="tag"),
    pytest.param(seed_groups, id="environment-group"),
    pytest.param(seed_settings, id="setting"),
    pytest.param(seed_team_memberships, id="membership-by-team"),
    pytest.param(seed_two_teams_for_one_user, id="membership-by-user"),
]

REPOSITORIES = [
    UserRepository,
    TeamRepository,
    TagRepository,
    EnvironmentGroupRepository,
    SettingRepository,
    TeamMembershipRepository,
]
LIST_METHODS = [
    pytest.param(repo, name, id=f"{repo.__name__}.{name}")
    for repo in REPOSITORIES
    for name in dir(repo)
    if name.startswith("list_")
]

NAMED_REPOSITORIES = [
    pytest.param(
        UserRepository,
        lambda repo, name: repo.create(name, PASSWORD_HASH, "standard"),
        "get_by_username",
        id="user",
    ),
    pytest.param(TeamRepository, lambda repo, name: repo.create(name), "get_by_name", id="team"),
    pytest.param(TagRepository, lambda repo, name: repo.create(name), "get_by_name", id="tag"),
    pytest.param(
        EnvironmentGroupRepository,
        lambda repo, name: repo.create(name),
        "get_by_name",
        id="environment-group",
    ),
]

RENAMES = [
    pytest.param(
        TeamRepository,
        lambda repo, name: repo.create(name),
        lambda repo, row_id, name: repo.rename(row_id, name),
        id="team",
    ),
    pytest.param(
        TagRepository,
        lambda repo, name: repo.create(name),
        lambda repo, row_id, name: repo.rename(row_id, name),
        id="tag",
    ),
    pytest.param(
        EnvironmentGroupRepository,
        lambda repo, name: repo.create(name),
        lambda repo, row_id, name: repo.update(row_id, name=name, description=None),
        id="environment-group",
    ),
]


async def make_user(repo: UserRepository, name: str = NAME) -> User:
    return await repo.create(name, PASSWORD_HASH, "standard")


@pytest.mark.parametrize(("repo", "method"), LIST_METHODS)
def test_every_list_method_takes_a_required_keyword_only_limit(repo: type, method: str) -> None:
    parameter = inspect.signature(getattr(repo, method)).parameters["limit"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_user_repr_does_not_contain_the_password_hash() -> None:
    user = User(1, NAME, PASSWORD_HASH, "standard", True, CREATED_STAMP, None)

    assert PASSWORD_HASH not in repr(user)


async def test_user_create_round_trips_every_field(user_repo: UserRepository) -> None:
    created = await user_repo.create(NAME, PASSWORD_HASH, "administrator")

    fetched = await user_repo.get(created.id)

    assert fetched == created
    assert created == User(
        created.id, NAME, PASSWORD_HASH, "administrator", True, CREATED_STAMP, None
    )


async def test_user_record_login_stamps_the_injected_clock(
    session: Session, user_repo: UserRepository
) -> None:
    user = await make_user(user_repo)

    await UserRepository(session, clock=at(LATER_AT)).record_login(user.id)

    fetched = await user_repo.get(user.id)
    assert fetched is not None
    assert fetched.last_login_at == LATER_STAMP


async def test_user_disable_marks_the_account_disabled(user_repo: UserRepository) -> None:
    user = await make_user(user_repo)

    changed = await user_repo.disable(user.id)

    fetched = await user_repo.get(user.id)
    assert changed is True
    assert fetched is not None
    assert fetched.is_enabled is False


async def test_user_create_stamps_utc_when_the_clock_has_another_offset(session: Session) -> None:
    repo = UserRepository(session, clock=at(LOCAL_MOMENT))

    created = await make_user(repo)

    assert created.created_at == CREATED_STAMP


async def test_user_get_for_an_unknown_id_returns_none(user_repo: UserRepository) -> None:
    assert await user_repo.get(UNKNOWN_ID) is None


async def test_user_delete_with_recorded_activity_raises_and_keeps_the_user(
    session: Session, user_repo: UserRepository
) -> None:
    user = await make_user(user_repo)
    await session.execute(ACTIVITY_INSERT, activity_row("user", user.id, None))

    with pytest.raises(UserHasActivityError):
        await user_repo.delete(user.id)

    assert await user_repo.get(user.id) == user


async def test_user_delete_without_activity_removes_the_user(user_repo: UserRepository) -> None:
    user = await make_user(user_repo)

    deleted = await user_repo.delete(user.id)

    assert deleted is True
    assert await user_repo.get(user.id) is None


async def test_user_delete_for_an_unknown_id_returns_false(user_repo: UserRepository) -> None:
    assert await user_repo.delete(UNKNOWN_ID) is False


async def test_team_create_round_trips_every_field(team_repo: TeamRepository) -> None:
    created = await team_repo.create(NAME)

    assert await team_repo.get(created.id) == created
    assert (created.name, created.created_at) == (NAME, CREATED_STAMP)


async def test_tag_create_round_trips_every_field(tag_repo: TagRepository) -> None:
    created = await tag_repo.create(NAME)

    assert await tag_repo.get(created.id) == created
    assert (created.name, created.created_at) == (NAME, CREATED_STAMP)


async def test_environment_group_create_round_trips_name_and_description(
    group_repo: EnvironmentGroupRepository,
) -> None:
    created = await group_repo.create(NAME, DESCRIPTION)

    assert await group_repo.get(created.id) == created
    assert created == EnvironmentGroup(created.id, NAME, DESCRIPTION, CREATED_STAMP)


async def test_environment_group_delete_leaves_its_environments_ungrouped(
    session: Session, group_repo: EnvironmentGroupRepository
) -> None:
    group = await group_repo.create(NAME)
    await session.execute(
        "INSERT INTO environment (name, type, connection_kind, socket_path, "
        "environment_group_id, created_at, updated_at) "
        "VALUES (?, 'standalone', 'unix_socket', '/var/run/docker.sock', ?, ?, ?)",
        (FIRST, group.id, CREATED_STAMP, CREATED_STAMP),
    )

    await group_repo.delete(group.id)

    row = await session.fetch_one("SELECT environment_group_id FROM environment")
    assert row is not None
    assert row[0] is None


async def test_setting_put_creates_a_setting_that_get_returns(
    setting_repo: SettingRepository,
) -> None:
    stored = await setting_repo.put(SETTING_KEY, "string", "dark")

    assert await setting_repo.get(SETTING_KEY) == stored
    assert (stored.key, stored.value_type, stored.value) == (SETTING_KEY, "string", "dark")


async def test_setting_put_on_an_existing_key_replaces_it_in_place(
    session: Session, setting_repo: SettingRepository
) -> None:
    original = await setting_repo.put(SETTING_KEY, "string", "dark")

    replaced = await SettingRepository(session, clock=at(LATER_AT)).put(SETTING_KEY, "integer", "3")

    assert replaced.id == original.id
    assert (replaced.value_type, replaced.value, replaced.updated_at) == (
        "integer",
        "3",
        LATER_STAMP,
    )


async def test_setting_get_matches_the_key_case_sensitively(
    setting_repo: SettingRepository,
) -> None:
    await setting_repo.put(SETTING_KEY, "string", "dark")

    assert await setting_repo.get(SETTING_KEY.upper()) is None


async def test_membership_add_round_trips_and_is_found_by_team_and_user(
    session: Session,
) -> None:
    repo, team, user = await seed_memberships(session)

    found = await repo.get_for(team.id, user.id)

    assert found is not None
    assert (found.team_id, found.user_id, found.is_leader) == (team.id, user.id, False)
    assert found.created_at == CREATED_STAMP
    assert await repo.get(found.id) == found


async def test_membership_list_for_team_excludes_other_teams(session: Session) -> None:
    repo, team, _ = await seed_memberships(session)
    other_team = await TeamRepository(session, clock=at(CREATED_AT)).create(SETTING_KEY)
    outsider = await UserRepository(session, clock=at(CREATED_AT)).create(
        SETTING_KEY, PASSWORD_HASH, "standard"
    )
    await repo.add(other_team.id, outsider.id)

    page = await repo.list_for_team(team.id, limit=10)

    assert {membership.team_id for membership in page.items} == {team.id}
    assert len(page.items) == 2


async def test_membership_list_for_user_excludes_other_users(session: Session) -> None:
    repo, _, user = await seed_memberships(session)

    page = await repo.list_for_user(user.id, limit=10)

    assert {membership.user_id for membership in page.items} == {user.id}
    assert len(page.items) == 1


async def test_membership_add_for_an_existing_pair_raises_duplicate_error(
    session: Session,
) -> None:
    repo, team, user = await seed_memberships(session)

    with pytest.raises(DuplicateError):
        await repo.add(team.id, user.id)


async def test_membership_add_for_an_unknown_team_raises_missing_reference_error(
    membership_repo: TeamMembershipRepository, user_repo: UserRepository
) -> None:
    user = await make_user(user_repo)

    with pytest.raises(MissingReferenceError):
        await membership_repo.add(UNKNOWN_ID, user.id)


async def test_membership_add_for_an_unknown_user_raises_missing_reference_error(
    membership_repo: TeamMembershipRepository, team_repo: TeamRepository
) -> None:
    team = await team_repo.create(NAME)

    with pytest.raises(MissingReferenceError):
        await membership_repo.add(team.id, UNKNOWN_ID)


async def test_team_delete_removes_its_memberships(session: Session) -> None:
    repo, team, user = await seed_memberships(session)

    await TeamRepository(session, clock=at(CREATED_AT)).delete(team.id)

    assert await repo.get_for(team.id, user.id) is None


@pytest.mark.parametrize(("repo_type", "create", "lookup"), NAMED_REPOSITORIES)
async def test_get_by_name_ignores_case(
    session: Session, repo_type: type, create: Callable[..., Awaitable[Any]], lookup: str
) -> None:
    repo = repo_type(session, clock=at(CREATED_AT))
    created = await create(repo, NAME)

    found = await getattr(repo, lookup)(NAME.upper())

    assert found == created


@pytest.mark.parametrize(("repo_type", "create", "_lookup"), NAMED_REPOSITORIES)
async def test_create_with_a_name_differing_only_by_case_raises_duplicate_error(
    session: Session, repo_type: type, create: Callable[..., Awaitable[Any]], _lookup: str
) -> None:
    repo = repo_type(session, clock=at(CREATED_AT))
    await create(repo, NAME)

    with pytest.raises(DuplicateError):
        await create(repo, NAME.lower())


@pytest.mark.parametrize(("repo_type", "create", "rename"), RENAMES)
async def test_rename_to_a_name_differing_only_by_case_raises_duplicate_error(
    session: Session,
    repo_type: type,
    create: Callable[..., Awaitable[Any]],
    rename: Callable[..., Awaitable[bool]],
) -> None:
    repo = repo_type(session, clock=at(CREATED_AT))
    await create(repo, NAME)
    other = await create(repo, SECOND)

    with pytest.raises(DuplicateError):
        await rename(repo, other.id, NAME.upper())


async def test_list_page_across_several_pages_yields_every_row_once_in_case_insensitive_order(
    team_repo: TeamRepository,
) -> None:
    for name in UNSHIFTED_NAMES:
        await team_repo.create(name)

    walk = await walk_pages(team_repo.list_page, limit=2, key=lambda team: team.name)

    assert walk.keys == CASE_INSENSITIVE_ORDER
    assert walk.page_sizes == [2, 2, 1]


async def test_list_page_with_exactly_one_full_page_returns_no_cursor(
    team_repo: TeamRepository,
) -> None:
    await team_repo.create(FIRST)
    await team_repo.create(SECOND)

    page = await team_repo.list_page(limit=2)

    assert len(page.items) == 2
    assert page.next_cursor is None


@pytest.mark.parametrize("seed", LIST_SEEDS)
async def test_list_page_with_limit_one_returns_a_cursor_then_the_remaining_row(
    session: Session, seed: Callable[[Session], Awaitable[ListPage]]
) -> None:
    list_page = await seed(session)

    first = await list_page(limit=1)
    second = await list_page(limit=1, cursor=first.next_cursor)

    assert len(first.items) == 1
    assert first.next_cursor is not None
    assert len(second.items) == 1
    assert second.next_cursor is None
    assert first.items != second.items


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1])
async def test_list_page_with_a_limit_out_of_range_raises_value_error(
    team_repo: TeamRepository, limit: int
) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        await team_repo.list_page(limit=limit)


async def test_list_page_with_a_tampered_cursor_raises_invalid_cursor_error(
    team_repo: TeamRepository,
) -> None:
    with pytest.raises(InvalidCursorError):
        await team_repo.list_page(limit=1, cursor="tampered")
