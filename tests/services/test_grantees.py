"""Splitting typed grantee names and resolving them to accounts."""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auth.errors import ValidationFailed
from app.db.engine import Database, Session, connect
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.services.grantees import (
    MAX_NAME_CHARS,
    MAX_NAMES_PER_FIELD,
    OWNER_GONE,
    GranteeDirectory,
    resolve_grantees,
    split_names,
)
from tests.db.helpers import CREATED_AT, CREATED_STAMP, PASSWORD_HASH, UNKNOWN_ID, at

ACCOUNT_COUNT = 600
LAST_ACCOUNT = f"member{ACCOUNT_COUNT - 1:03d}"
COMMA_TEAM = "Ops, Dev"
PLAIN_TEAM = "platform"
OWNER = "Alice"
TEAM_NAMES = ("alpha", "bravo", "charlie")
TEAM_TABLE_READ = "FROM team"

AT_THE_LIMIT = ",".join(f"name{number}" for number in range(MAX_NAMES_PER_FIELD))
OVER_THE_LIMIT = AT_THE_LIMIT + ",one-too-many"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a,b\nc", ("a", "b", "c")),
        ("  a  ,\r\n b \rc", ("a", "b", "c")),
        ("a,,\n\n  ,b", ("a", "b")),
        ("Alice, alice\nALICE", ("Alice",)),
        ("É, é", ("É", "é")),
        ("", ()),
        ("   \n , ", ()),
    ],
    ids=[
        "commas-and-new-lines",
        "stripped-and-carriage-returns",
        "empty-parts-dropped",
        "ascii-case-repeats-kept-once",
        "non-ascii-case-kept-apart",
        "empty-text",
        "only-separators",
    ],
)
def test_split_names_returns_each_distinct_stripped_name_in_order(
    text: str, expected: tuple[str, ...]
) -> None:
    assert split_names(text) == expected


def test_split_names_without_commas_keeps_a_comma_inside_a_name() -> None:
    assert split_names("Ops, Dev\nplatform", commas=False) == ("Ops, Dev", "platform")


def test_split_names_accepts_exactly_the_limit() -> None:
    assert len(split_names(AT_THE_LIMIT)) == MAX_NAMES_PER_FIELD


def test_split_names_refuses_more_than_the_limit_under_the_given_field() -> None:
    with pytest.raises(ValidationFailed) as refused:
        split_names(OVER_THE_LIMIT, field="add_users")

    assert refused.value.fields == {
        "add_users": f"Name at most {MAX_NAMES_PER_FIELD} in one field."
    }


def test_split_names_counts_a_repeat_once_towards_the_limit() -> None:
    assert len(split_names(AT_THE_LIMIT + "," + AT_THE_LIMIT.upper())) == MAX_NAMES_PER_FIELD


@pytest.fixture(scope="module")
def directory_file(migrated_template: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build once a database of ACCOUNT_COUNT users, the owner, and two teams."""
    path = tmp_path_factory.mktemp("grantees") / "dockmin.db"
    shutil.copyfile(migrated_template, path)
    usernames = [f"member{number:03d}" for number in range(ACCOUNT_COUNT)] + [OWNER]
    connection = connect(path)
    try:
        connection.executemany(
            "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
            "VALUES (?, ?, 'standard', 1, ?)",
            [(name, PASSWORD_HASH, CREATED_STAMP) for name in usernames],
        )
        connection.executemany(
            "INSERT INTO team (name, created_at) VALUES (?, ?)",
            [(COMMA_TEAM, CREATED_STAMP), (PLAIN_TEAM, CREATED_STAMP)],
        )
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
async def session(directory_file: Path) -> AsyncIterator[Session]:
    async with Database(directory_file).session() as opened:
        yield opened


@pytest.fixture
def services(session: Session) -> SimpleNamespace:
    clock = at(CREATED_AT)
    return SimpleNamespace(
        grantees=GranteeDirectory(
            UserRepository(session, clock=clock), TeamRepository(session, clock=clock)
        )
    )


async def test_resolving_returns_the_ids_of_users_teams_and_owner(
    services: SimpleNamespace, session: Session
) -> None:
    users = UserRepository(session, clock=at(CREATED_AT))
    first, owner = await users.get_by_username("member000"), await users.get_by_username(OWNER)
    assert first is not None and owner is not None

    user_ids, team_ids, owner_id = await resolve_grantees(
        services, users=["MEMBER000", "member000"], teams=[PLAIN_TEAM.upper()], owner=f"  {OWNER} "
    )

    assert (user_ids, owner_id) == ((first.id,), owner.id)
    assert len(team_ids) == 1


async def test_a_team_name_containing_a_comma_resolves_when_split_on_new_lines_only(
    services: SimpleNamespace,
) -> None:
    names = split_names(f"{COMMA_TEAM}\n{PLAIN_TEAM}", commas=False)

    _, team_ids, _ = await resolve_grantees(services, users=[], teams=names, owner="")

    assert len(team_ids) == 2


async def test_a_blank_owner_resolves_to_no_owner(services: SimpleNamespace) -> None:
    _, _, owner_id = await resolve_grantees(services, users=[], teams=[], owner="  ")

    assert owner_id is None


async def test_the_last_of_more_than_five_hundred_accounts_resolves_by_name(
    services: SimpleNamespace,
) -> None:
    user_ids, _, owner_id = await resolve_grantees(
        services, users=[LAST_ACCOUNT], teams=[], owner=LAST_ACCOUNT
    )

    assert len(user_ids) == 1
    assert owner_id == user_ids[0]


async def test_a_given_owner_id_is_the_owner_whatever_name_is_typed_and_must_name_an_account(
    services: SimpleNamespace, session: Session
) -> None:
    owner = await UserRepository(session, clock=at(CREATED_AT)).get_by_username(OWNER)
    assert owner is not None

    _, _, kept = await resolve_grantees(
        services, users=[], teams=[], owner="someone else", owner_id=owner.id
    )
    with pytest.raises(ValidationFailed) as refused:
        await resolve_grantees(services, users=[], teams=[], owner="", owner_id=UNKNOWN_ID)

    assert kept == owner.id
    assert refused.value.fields == {"owner_name": OWNER_GONE}


async def test_every_unknown_name_is_reported_under_the_field_that_carried_it(
    services: SimpleNamespace,
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await resolve_grantees(
            services,
            users=["member000", "nobody", "ghost"],
            teams=["void"],
            owner="phantom",
        )

    assert refused.value.fields == {
        "add_users": "No users are named “nobody”, “ghost”. Check the spelling.",
        "add_teams": "No team is named “void”. Check the spelling.",
        "owner_name": "No user is named “phantom”. Check the spelling.",
    }


async def test_only_the_first_five_unknown_names_are_listed_and_the_rest_counted(
    services: SimpleNamespace,
) -> None:
    unknown = [f"absent{number}" for number in range(7)]

    with pytest.raises(ValidationFailed) as refused:
        await resolve_grantees(services, users=unknown, teams=[], owner="")

    assert refused.value.fields["add_users"] == (
        "No users are named “absent0”, “absent1”, “absent2”, “absent3”, “absent4” and 2 more. "
        "Check the spelling."
    )


async def test_a_name_over_the_length_limit_is_unknown_and_shown_truncated_and_printable(
    services: SimpleNamespace,
) -> None:
    overlong = "x" * (MAX_NAME_CHARS + 1)
    control_character = "bad\x00name"

    with pytest.raises(ValidationFailed) as refused:
        await resolve_grantees(services, users=[overlong, control_character], teams=[], owner="")

    assert refused.value.fields["add_users"] == (
        f"No users are named “{'x' * MAX_NAME_CHARS}”, “badname”. Check the spelling."
    )


@pytest.fixture
async def team_directory(
    database: Database,
) -> AsyncIterator[tuple[GranteeDirectory, list[int], Session]]:
    async with database.session() as opened:
        clock = at(CREATED_AT)
        teams = TeamRepository(opened, clock=clock)
        ids = [(await teams.create(name)).id for name in TEAM_NAMES]
        yield GranteeDirectory(UserRepository(opened, clock=clock), teams), ids, opened


def team_reads(statements: list[str]) -> list[str]:
    return [statement for statement in statements if TEAM_TABLE_READ in statement]


async def test_names_returns_each_existing_team_and_omits_a_missing_id_in_one_team_query(
    team_directory: tuple[GranteeDirectory, list[int], Session],
) -> None:
    directory, ids, session = team_directory
    statements: list[str] = []
    session._connection.set_trace_callback(statements.append)

    found = await directory.names(user_ids=[], team_ids=[*ids, UNKNOWN_ID], owner_id=None)

    session._connection.set_trace_callback(None)
    assert found.teams == dict(zip(ids, TEAM_NAMES, strict=True))
    assert len(team_reads(statements)) == 1


async def test_names_with_no_team_ids_returns_an_empty_mapping(
    team_directory: tuple[GranteeDirectory, list[int], Session],
) -> None:
    directory, _, _ = team_directory

    found = await directory.names(user_ids=[], team_ids=[], owner_id=None)

    assert found.teams == {}
