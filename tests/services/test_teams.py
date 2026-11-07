"""Team management: administrators run teams, a leader runs the membership of their own."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, TeamNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.pagination import Page
from app.db.repositories.user import UserRepository
from app.services.assembly import AccessServices
from app.services.teams import TEAM_NAME_MAX
from tests.access_support import (
    ADMIN,
    LEADER,
    LEADER_ID,
    MEMBER,
    MEMBER_ID,
    OTHER_TEAM_NAME,
    PEER,
    PEER_ID,
    STANDARD,
    TEAM_NAME,
    UNKNOWN_TEAM_ID,
    UNKNOWN_USER_ID,
    team_of,
)
from tests.auth_support import FakeClock

PAGE = 50

Operation = Callable[[AccessServices, int], Awaitable[object]]


@pytest.fixture
async def team_id(access: AccessServices) -> int:
    """A team led by LEADER, with MEMBER on it."""
    created = await team_of(access, TEAM_NAME, LEADER, MEMBER)
    await access.teams.set_leader(ADMIN, created, user_id=LEADER_ID, is_leader=True)
    return created


@pytest.fixture
async def other_team_id(access: AccessServices) -> int:
    """A team LEADER does not belong to, with PEER on it."""
    return await team_of(access, OTHER_TEAM_NAME, PEER)


def usernames(page: Page[Any]) -> list[str]:
    return [entry.username for entry in page.items]


async def test_an_administrator_creates_a_team(access: AccessServices) -> None:
    team = await access.teams.create(ADMIN, name=f"  {TEAM_NAME}  ")

    assert team.name == TEAM_NAME


@pytest.mark.parametrize(
    "operation",
    [
        lambda access, team: access.teams.create(STANDARD, name="new"),
        lambda access, team: access.teams.rename(STANDARD, team, name="new"),
        lambda access, team: access.teams.delete(STANDARD, team, expected_name=TEAM_NAME),
        lambda access, team: access.teams.set_leader(STANDARD, team, user_id=1, is_leader=True),
    ],
    ids=["create", "rename", "delete", "set_leader"],
)
async def test_only_an_administrator_may_create_rename_delete_or_appoint(
    access: AccessServices, team_id: int, operation: Operation
) -> None:
    with pytest.raises(PermissionDenied):
        await operation(access, team_id)


async def test_a_team_name_already_taken_in_any_case_is_refused(access: AccessServices) -> None:
    await access.teams.create(ADMIN, name=TEAM_NAME)

    with pytest.raises(ValidationFailed) as refused:
        await access.teams.create(ADMIN, name=TEAM_NAME.upper())

    assert "name" in refused.value.fields


@pytest.mark.parametrize("name", ["", "   ", "x" * (TEAM_NAME_MAX + 1), "bad\x00name", "tab\tname"])
async def test_a_team_name_that_is_blank_too_long_or_unprintable_is_refused(
    access: AccessServices, name: str
) -> None:
    with pytest.raises(ValidationFailed):
        await access.teams.create(ADMIN, name=name)


async def test_renaming_to_a_name_another_team_holds_is_refused(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    with pytest.raises(ValidationFailed):
        await access.teams.rename(ADMIN, team_id, name=OTHER_TEAM_NAME)


async def test_renaming_a_team_changes_its_name(access: AccessServices, team_id: int) -> None:
    team = await access.teams.rename(ADMIN, team_id, name="renamed")

    assert team.name == "renamed"


async def test_renaming_or_deleting_a_team_that_does_not_exist_is_not_found(
    access: AccessServices,
) -> None:
    with pytest.raises(TeamNotFound):
        await access.teams.delete(ADMIN, UNKNOWN_TEAM_ID, expected_name=TEAM_NAME)


async def test_deleting_a_team_removes_its_memberships(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.delete(ADMIN, team_id, expected_name=TEAM_NAME)

    remaining = await access.teams.list_page(MEMBER, limit=PAGE, cursor=None)
    assert remaining.items == []


async def test_an_administrator_lists_every_team(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    page = await access.teams.list_page(ADMIN, limit=PAGE, cursor=None)

    assert sorted(team.name for team in page.items) == [TEAM_NAME, OTHER_TEAM_NAME]


async def test_a_standard_user_lists_only_the_teams_they_belong_to(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    page = await access.teams.list_page(MEMBER, limit=PAGE, cursor=None)

    assert [team.name for team in page.items] == [TEAM_NAME]


async def test_a_member_sees_their_team_but_may_not_manage_it(
    access: AccessServices, team_id: int
) -> None:
    detail = await access.teams.get(MEMBER, team_id)

    assert (
        detail.can_manage_members,
        detail.can_manage_leaders,
        detail.can_rename_and_delete,
    ) == (False, False, False)


async def test_a_leader_may_manage_members_but_not_leaders_or_the_team_itself(
    access: AccessServices, team_id: int
) -> None:
    detail = await access.teams.get(LEADER, team_id)

    assert (
        detail.can_manage_members,
        detail.can_manage_leaders,
        detail.can_rename_and_delete,
    ) == (True, False, False)


async def test_an_administrator_may_do_everything_to_a_team(
    access: AccessServices, team_id: int
) -> None:
    detail = await access.teams.get(ADMIN, team_id)

    assert (
        detail.can_manage_members,
        detail.can_manage_leaders,
        detail.can_rename_and_delete,
    ) == (True, True, True)


async def test_a_team_the_user_does_not_belong_to_is_indistinguishable_from_a_missing_one(
    access: AccessServices, other_team_id: int
) -> None:
    with pytest.raises(TeamNotFound) as hidden:
        await access.teams.get(MEMBER, other_team_id)
    with pytest.raises(TeamNotFound) as missing:
        await access.teams.get(MEMBER, UNKNOWN_TEAM_ID)

    assert type(hidden.value) is type(missing.value)


async def test_a_member_lists_the_members_of_their_team(
    access: AccessServices, team_id: int
) -> None:
    page = await access.teams.list_members(MEMBER, team_id, limit=PAGE, cursor=None)

    assert usernames(page) == [LEADER.username, MEMBER.username]


async def test_a_non_member_cannot_list_a_teams_members(
    access: AccessServices, other_team_id: int
) -> None:
    with pytest.raises(TeamNotFound):
        await access.teams.list_members(MEMBER, other_team_id, limit=PAGE, cursor=None)


async def test_a_leader_can_add_a_member_to_their_own_team(
    access: AccessServices, team_id: int
) -> None:
    entry = await access.teams.add_member(LEADER, team_id, username=PEER.username)

    assert (entry.user_id, entry.is_leader) == (PEER_ID, False)


async def test_a_leader_can_remove_a_member_from_their_own_team(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.remove_member(LEADER, team_id, user_id=MEMBER_ID)

    page = await access.teams.list_members(LEADER, team_id, limit=PAGE, cursor=None)
    assert usernames(page) == [LEADER.username]


async def test_a_leader_cannot_add_a_member_to_another_team(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    with pytest.raises(TeamNotFound):
        await access.teams.add_member(LEADER, other_team_id, username=MEMBER.username)


async def test_a_leader_cannot_remove_a_member_from_another_team(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    with pytest.raises(TeamNotFound):
        await access.teams.remove_member(LEADER, other_team_id, user_id=PEER_ID)


async def test_a_leader_of_one_team_cannot_manage_another_team_they_merely_belong_to(
    access: AccessServices, team_id: int, other_team_id: int
) -> None:
    await access.teams.add_member(ADMIN, other_team_id, username=LEADER.username)

    with pytest.raises(PermissionDenied):
        await access.teams.add_member(LEADER, other_team_id, username=MEMBER.username)


async def test_an_ordinary_member_cannot_change_membership(
    access: AccessServices, team_id: int
) -> None:
    with pytest.raises(PermissionDenied):
        await access.teams.add_member(MEMBER, team_id, username=PEER.username)


async def test_a_leader_cannot_appoint_another_leader(access: AccessServices, team_id: int) -> None:
    with pytest.raises(PermissionDenied):
        await access.teams.add_member(LEADER, team_id, username=PEER.username, is_leader=True)


async def test_a_leader_cannot_remove_a_leader(access: AccessServices, team_id: int) -> None:
    with pytest.raises(PermissionDenied):
        await access.teams.remove_member(LEADER, team_id, user_id=LEADER_ID)


async def test_an_administrator_can_appoint_and_remove_a_leader(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.add_member(ADMIN, team_id, username=PEER.username, is_leader=True)

    await access.teams.remove_member(ADMIN, team_id, user_id=PEER_ID)

    page = await access.teams.list_members(ADMIN, team_id, limit=PAGE, cursor=None)
    assert PEER.username not in usernames(page)


async def test_a_leader_who_has_been_dismissed_can_no_longer_change_membership(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.set_leader(ADMIN, team_id, user_id=LEADER_ID, is_leader=False)

    with pytest.raises(PermissionDenied):
        await access.teams.add_member(LEADER, team_id, username=PEER.username)


async def test_a_leader_who_has_been_removed_no_longer_sees_the_team(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.remove_member(ADMIN, team_id, user_id=LEADER_ID)

    with pytest.raises(TeamNotFound):
        await access.teams.add_member(LEADER, team_id, username=PEER.username)


async def test_adding_a_user_who_does_not_exist_is_refused(
    access: AccessServices, team_id: int
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await access.teams.add_member(ADMIN, team_id, username="nobody-by-that-name")

    assert "username" in refused.value.fields


async def test_adding_a_disabled_user_is_refused(
    access: AccessServices, db: Session, clock: FakeClock, team_id: int
) -> None:
    await UserRepository(db, clock).disable(PEER_ID)

    with pytest.raises(ValidationFailed):
        await access.teams.add_member(ADMIN, team_id, username=PEER.username)


async def test_adding_a_user_who_is_already_a_member_is_refused(
    access: AccessServices, team_id: int
) -> None:
    with pytest.raises(ValidationFailed):
        await access.teams.add_member(ADMIN, team_id, username=MEMBER.username)


async def test_removing_a_user_who_is_not_a_member_is_refused(
    access: AccessServices, team_id: int
) -> None:
    with pytest.raises(ValidationFailed):
        await access.teams.remove_member(ADMIN, team_id, user_id=PEER_ID)


async def test_appointing_a_leader_who_is_not_a_member_is_refused(
    access: AccessServices, team_id: int
) -> None:
    with pytest.raises(ValidationFailed):
        await access.teams.set_leader(ADMIN, team_id, user_id=UNKNOWN_USER_ID, is_leader=True)


async def test_deleting_a_team_that_was_renamed_since_the_review_is_refused_and_deletes_nothing(
    access: AccessServices, team_id: int
) -> None:
    await access.teams.rename(ADMIN, team_id, name="renamed-after-review")

    with pytest.raises(ValidationFailed) as refused:
        await access.teams.delete(ADMIN, team_id, expected_name=TEAM_NAME)

    assert "name" in refused.value.fields
    assert [
        team.name for team in (await access.teams.list_page(ADMIN, limit=PAGE, cursor=None)).items
    ] == ["renamed-after-review"]
